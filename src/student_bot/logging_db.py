"""SQLite-backed Q&A and feedback log.

User IDs are stored as salted SHA-256 hashes. The salt lives in the .env file
and never enters the database, so a leaked DB cannot be reverse-mapped to
Mattermost user IDs without it.

Two privacy primitives the bot relies on:
- `disclosed`     — has this user been shown the GDPR notice?
- `logging_opt_out` — has this user asked us to stop logging their content?

When a user has opted out, qa_log writes are skipped and only an anonymous
counter row is recorded so analytics can still see *that* a question
happened (without its content).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from student_bot.config import Config


SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    user_id_hash TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    channel_id TEXT,
    bot_post_id TEXT,
    root_id TEXT,
    question TEXT NOT NULL,
    lang TEXT NOT NULL,
    retrieved_chunk_ids TEXT NOT NULL,
    rerank_top1 REAL NOT NULL,
    rerank_meanK REAL NOT NULL,
    distinct_sources INTEGER NOT NULL,
    gate_pass INTEGER NOT NULL,
    gate_reason TEXT NOT NULL,
    answer TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    topic TEXT,
    topic_confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_qa_log_user ON qa_log(user_id_hash);
CREATE INDEX IF NOT EXISTS idx_qa_log_botpost ON qa_log(bot_post_id);
CREATE INDEX IF NOT EXISTS idx_qa_log_ts ON qa_log(ts);
CREATE INDEX IF NOT EXISTS idx_qa_log_topic ON qa_log(topic);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    qa_id INTEGER,
    bot_post_id TEXT NOT NULL,
    user_id_hash TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    emoji TEXT NOT NULL,
    FOREIGN KEY (qa_id) REFERENCES qa_log(id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_botpost ON feedback(bot_post_id);
CREATE INDEX IF NOT EXISTS idx_feedback_qa ON feedback(qa_id);

CREATE TABLE IF NOT EXISTS disclosed (
    user_id_hash TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS logging_opt_out (
    user_id_hash TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);

-- Counter for opted-out users so analytics still see traffic volume even
-- when content is not stored.
CREATE TABLE IF NOT EXISTS anon_counter (
    bucket_ts INTEGER NOT NULL,   -- floor(ts / 3600) * 3600
    lang TEXT NOT NULL,
    gate_pass INTEGER NOT NULL,
    n INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (bucket_ts, lang, gate_pass)
);
"""


# Migrations applied on startup. Idempotent — uses PRAGMA table_info checks.
MIGRATIONS: list[tuple[str, str]] = [
    ("qa_log.topic", "ALTER TABLE qa_log ADD COLUMN topic TEXT"),
    ("qa_log.topic_confidence", "ALTER TABLE qa_log ADD COLUMN topic_confidence REAL"),
    # Original `question` stays the user's typed text (for analytics).
    # `question_expanded` captures the post-jargon-expansion query that was
    # actually fed to retrieval. Useful for debugging "why did this query miss?"
    ("qa_log.question_expanded", "ALTER TABLE qa_log ADD COLUMN question_expanded TEXT"),
    # Comma-separated list of jargon term keys hit by this query.
    ("qa_log.jargon_hits", "ALTER TABLE qa_log ADD COLUMN jargon_hits TEXT"),
    # Coarse token estimates (chars/4) captured at answer time. Historical
    # rows may stay NULL until backfilled by scripts/backfill_tokens.py.
    ("qa_log.prompt_tokens", "ALTER TABLE qa_log ADD COLUMN prompt_tokens INTEGER"),
    ("qa_log.gen_tokens", "ALTER TABLE qa_log ADD COLUMN gen_tokens INTEGER"),
    # Per-request streaming metrics: time-to-first-token and tokens/sec during
    # generation. Pipeline already computes these; previously dropped at write.
    ("qa_log.ttft_ms", "ALTER TABLE qa_log ADD COLUMN ttft_ms INTEGER"),
    ("qa_log.gen_tps", "ALTER TABLE qa_log ADD COLUMN gen_tps REAL"),
    # Ollama model that produced this answer. Stored as the configured model
    # tag so future histograms can split by model.
    ("qa_log.llm_model", "ALTER TABLE qa_log ADD COLUMN llm_model TEXT"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


class LogDB:
    def __init__(self, cfg: Config):
        self.path: Path = cfg.absolute(cfg.paths.logs_db)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.salt = (cfg.user_id_hash_salt or "").encode("utf-8")
        if not self.salt:
            self.salt = b"unsalted-cli-only"
        self._lock = Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            for spec, sql in MIGRATIONS:
                table, column = spec.split(".")
                if not _column_exists(conn, table, column):
                    conn.execute(sql)
            # One-shot backfill: pre-existing rows have no llm_model. Issue
            # #58 notes only one LLM has ever been in production, so filling
            # NULL rows with the currently-configured model is accurate. This
            # is a no-op once every row has a value, so leaving it
            # unconditional avoids needing a separate migration tracker.
            try:
                current_model = (cfg.llm.model or "").strip()
            except AttributeError:
                current_model = ""
            if current_model:
                conn.execute(
                    "UPDATE qa_log SET llm_model = ? WHERE llm_model IS NULL",
                    (current_model,),
                )
            conn.commit()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.path))
        try:
            yield conn
        finally:
            conn.close()

    def hash_user(self, user_id: str) -> str:
        h = hashlib.sha256()
        h.update(self.salt)
        h.update(b"\0")
        h.update(user_id.encode("utf-8"))
        return h.hexdigest()

    # --- disclosure ---

    def has_disclosed(self, user_id: str) -> bool:
        uid = self.hash_user(user_id)
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM disclosed WHERE user_id_hash = ?", (uid,)).fetchone()
        return row is not None

    def mark_disclosed(self, user_id: str) -> None:
        uid = self.hash_user(user_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO disclosed(user_id_hash, ts) VALUES (?, ?)",
                (uid, int(time.time())),
            )
            conn.commit()

    # --- opt-out ---

    def is_opted_out(self, user_id: str) -> bool:
        uid = self.hash_user(user_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM logging_opt_out WHERE user_id_hash = ?", (uid,)
            ).fetchone()
        return row is not None

    def set_opt_out(self, user_id: str, opted_out: bool) -> None:
        uid = self.hash_user(user_id)
        with self._lock, self._connect() as conn:
            if opted_out:
                conn.execute(
                    "INSERT OR REPLACE INTO logging_opt_out(user_id_hash, ts) VALUES (?, ?)",
                    (uid, int(time.time())),
                )
            else:
                conn.execute("DELETE FROM logging_opt_out WHERE user_id_hash = ?", (uid,))
            conn.commit()

    def record_anon(self, lang: str, gate_pass: bool) -> None:
        bucket = (int(time.time()) // 3600) * 3600
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO anon_counter(bucket_ts, lang, gate_pass, n)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(bucket_ts, lang, gate_pass)
                DO UPDATE SET n = n + 1
                """,
                (bucket, lang, 1 if gate_pass else 0),
            )
            conn.commit()

    # --- qa_log ---

    def record_qa(
        self,
        *,
        user_id: str,
        channel_type: str,
        channel_id: str | None,
        bot_post_id: str | None,
        root_id: str | None,
        question: str,
        lang: str,
        retrieved_chunk_ids: list[str],
        rerank_top1: float,
        rerank_meanK: float,
        distinct_sources: int,
        gate_pass: bool,
        gate_reason: str,
        answer: str,
        latency_ms: int,
        question_expanded: str | None = None,
        jargon_hits: list[str] | None = None,
        prompt_tokens: int | None = None,
        gen_tokens: int | None = None,
        ttft_ms: int | None = None,
        gen_tps: float | None = None,
        llm_model: str | None = None,
    ) -> int | None:
        """Record a Q&A row, or skip (returning None) when the user opted out.

        For opted-out users we still bump the anonymous hourly counter so the
        operator can see request volume without content."""
        if self.is_opted_out(user_id):
            self.record_anon(lang, gate_pass)
            return None

        uid = self.hash_user(user_id)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO qa_log(
                    ts, user_id_hash, channel_type, channel_id, bot_post_id, root_id,
                    question, lang, retrieved_chunk_ids,
                    rerank_top1, rerank_meanK, distinct_sources,
                    gate_pass, gate_reason, answer, latency_ms,
                    question_expanded, jargon_hits,
                    prompt_tokens, gen_tokens,
                    ttft_ms, gen_tps, llm_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    uid,
                    channel_type,
                    channel_id,
                    bot_post_id,
                    root_id,
                    question,
                    lang,
                    json.dumps(retrieved_chunk_ids),
                    rerank_top1,
                    rerank_meanK,
                    distinct_sources,
                    1 if gate_pass else 0,
                    gate_reason,
                    answer,
                    latency_ms,
                    question_expanded,
                    ",".join(jargon_hits or []) if jargon_hits else None,
                    prompt_tokens,
                    gen_tokens,
                    ttft_ms,
                    gen_tps,
                    llm_model,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def update_topic(self, qa_id: int, topic: str, confidence: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE qa_log SET topic = ?, topic_confidence = ? WHERE id = ?",
                (topic, confidence, qa_id),
            )
            conn.commit()

    def lookup_qa_by_bot_post(self, bot_post_id: str) -> int | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM qa_log WHERE bot_post_id = ? LIMIT 1", (bot_post_id,)
            ).fetchone()
        return int(row[0]) if row else None

    # --- feedback ---

    def record_feedback(
        self,
        *,
        bot_post_id: str,
        user_id: str,
        sentiment: str,
        emoji: str,
    ) -> None:
        qa_id = self.lookup_qa_by_bot_post(bot_post_id)
        uid = self.hash_user(user_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback(ts, qa_id, bot_post_id, user_id_hash, sentiment, emoji)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(time.time()), qa_id, bot_post_id, uid, sentiment, emoji),
            )
            conn.commit()

    # --- analytics helpers ---

    @staticmethod
    def _channel_clause(channel: str | None) -> tuple[str, tuple]:
        """Return (sql_fragment, params) for filtering qa_log by channel.

        channel: None or 'all' → no filter; 'web' → channel_type = 'W';
        'mm' → channel_type != 'W' (Mattermost rows use D/O/P/G/...).
        Fragment is empty or starts with ' AND '.
        """
        if channel in (None, "all"):
            return "", ()
        if channel == "web":
            return " AND q.channel_type = ?", ("W",)
        if channel == "mm":
            return " AND q.channel_type != ?", ("W",)
        raise ValueError(f"unknown channel filter: {channel!r}")

    def stats_by_topic(self, since_ts: int = 0, channel: str | None = None) -> list[dict]:
        ch_sql, ch_params = self._channel_clause(channel)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    COALESCE(q.topic, 'unclassified') AS topic,
                    COUNT(*) AS n,
                    SUM(CASE WHEN q.gate_pass = 1 THEN 1 ELSE 0 END) AS answered,
                    AVG(q.latency_ms) AS avg_latency_ms,
                    SUM(CASE WHEN f.sentiment = 'positive' THEN 1 ELSE 0 END) AS thumbs_up,
                    SUM(CASE WHEN f.sentiment = 'negative' THEN 1 ELSE 0 END) AS thumbs_down,
                    COUNT(DISTINCT f.id) AS feedback_count
                FROM qa_log q
                LEFT JOIN feedback f ON f.qa_id = q.id
                WHERE q.ts >= ?{ch_sql}
                GROUP BY topic
                ORDER BY n DESC
                """,
                (since_ts, *ch_params),
            ).fetchall()
        return [
            {
                "topic": r[0],
                "n": r[1],
                "answered": r[2],
                "avg_latency_ms": int(r[3] or 0),
                "thumbs_up": r[4] or 0,
                "thumbs_down": r[5] or 0,
                "feedback_count": r[6] or 0,
            }
            for r in rows
        ]

    def overall_counts(self, since_ts: int = 0, channel: str | None = None) -> dict:
        ch_sql, ch_params = self._channel_clause(channel)
        # `q.` alias is required by _channel_clause but the inner query has no
        # join, so we alias the table.
        with self._lock, self._connect() as conn:
            r = conn.execute(
                f"""
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN gate_pass = 1 THEN 1 ELSE 0 END),
                    AVG(latency_ms)
                FROM qa_log q WHERE q.ts >= ?{ch_sql}
                """,
                (since_ts, *ch_params),
            ).fetchone()
            # anon_counter has no channel column — only show its total when
            # the user is viewing unfiltered stats.
            if channel in (None, "all"):
                anon = conn.execute(
                    "SELECT COALESCE(SUM(n), 0) FROM anon_counter WHERE bucket_ts >= ?",
                    (since_ts,),
                ).fetchone()[0]
            else:
                anon = 0
        return {
            "logged": int(r[0] or 0),
            "answered": int(r[1] or 0),
            "avg_latency_ms": int(r[2] or 0),
            "anon": int(anon),
        }

    def series_buckets(
        self,
        since_ts: int,
        bucket_seconds: int,
        channel: str | None = None,
    ) -> list[dict]:
        """Group qa_log into fixed-width time buckets for the stats page chart.

        Returns one dict per bucket present in the window, sorted ascending by
        timestamp. Buckets with zero rows are omitted; the front-end fills gaps.
        Token columns coalesce NULL → 0 so old (pre-migration) rows count as
        zero-token rather than breaking the SUM. `channel` filters to web-only
        or Mattermost-only rows.
        """
        if bucket_seconds <= 0:
            raise ValueError("bucket_seconds must be positive")
        ch_sql, ch_params = self._channel_clause(channel)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    (q.ts / ?) * ?                                AS bucket_ts,
                    COUNT(*)                                      AS n,
                    SUM(CASE WHEN q.gate_pass = 1 THEN 1 ELSE 0 END) AS n_answered,
                    SUM(COALESCE(q.prompt_tokens, 0))             AS prompt_tokens,
                    SUM(COALESCE(q.gen_tokens, 0))                AS gen_tokens,
                    SUM(CASE WHEN f.sentiment = 'positive' THEN 1 ELSE 0 END) AS thumbs_up,
                    SUM(CASE WHEN f.sentiment = 'negative' THEN 1 ELSE 0 END) AS thumbs_down
                FROM qa_log q
                LEFT JOIN feedback f ON f.qa_id = q.id
                WHERE q.ts >= ?{ch_sql}
                GROUP BY bucket_ts
                ORDER BY bucket_ts ASC
                """,
                (bucket_seconds, bucket_seconds, since_ts, *ch_params),
            ).fetchall()
        return [
            {
                "bucket_ts": int(r[0]),
                "n": int(r[1] or 0),
                "n_answered": int(r[2] or 0),
                "prompt_tokens": int(r[3] or 0),
                "gen_tokens": int(r[4] or 0),
                "thumbs_up": int(r[5] or 0),
                "thumbs_down": int(r[6] or 0),
            }
            for r in rows
        ]

    def series_rows(
        self,
        since_ts: int,
        channel: str | None = None,
    ) -> list[dict]:
        """Per-row metrics for the stats-page histograms.

        Only returns gate_pass=1 rows because refused turns have no
        prompt/gen token counts and no streaming metrics — including them
        would just inject a NULL/zero spike at the histogram's left edge.
        Caller (JS) bins these client-side over 50 fixed-width bins.
        """
        ch_sql, ch_params = self._channel_clause(channel)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    q.prompt_tokens,
                    q.gen_tokens,
                    q.ttft_ms,
                    q.gen_tps,
                    q.llm_model
                FROM qa_log q
                WHERE q.ts >= ? AND q.gate_pass = 1{ch_sql}
                ORDER BY q.ts ASC
                """,
                (since_ts, *ch_params),
            ).fetchall()
        return [
            {
                "prompt_tokens": int(r[0]) if r[0] is not None else None,
                "gen_tokens": int(r[1]) if r[1] is not None else None,
                "ttft_ms": int(r[2]) if r[2] is not None else None,
                "gen_tps": float(r[3]) if r[3] is not None else None,
                "llm_model": r[4],
            }
            for r in rows
        ]

    def activity_for_users(self, user_id_hashes: list[str], since_ts: int = 0) -> dict[str, dict]:
        """Return {hash: {n_qa, last_ts}} for hashes with any qa_log rows.

        Hashes with no activity in the window are omitted. Used by the stats
        page to show which registered web users have actually used the bot.
        """
        if not user_id_hashes:
            return {}
        placeholders = ",".join("?" * len(user_id_hashes))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT user_id_hash, COUNT(*), MAX(ts)
                FROM qa_log
                WHERE ts >= ? AND user_id_hash IN ({placeholders})
                GROUP BY user_id_hash
                """,
                (since_ts, *user_id_hashes),
            ).fetchall()
        return {r[0]: {"n_qa": int(r[1] or 0), "last_ts": int(r[2] or 0)} for r in rows}


__all__ = ["LogDB"]
