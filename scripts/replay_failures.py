"""student-bot-replay-failures: replay historically-failing questions
against the *current* corpus and report what changed.

Pulls rows from `data/logs.sqlite` matching some failure signal (refusal,
low-confidence pass, or thumbs-down feedback), re-runs retrieval + gate
(and optionally the LLM) using the live config, and prints a per-row diff
plus a fixed/unchanged/regressed tally.

Use after a corpus expansion, gate-threshold change, or prompt change to
see whether real past failures are now answered correctly. Read-only;
does not modify the index, the manifest, or the log.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from student_bot.bot.gate import evaluate as evaluate_gate
from student_bot.bot.pipeline import answer as run_answer
from student_bot.bot.retrieval import retrieve
from student_bot.config import get_config


# Window for "is this row a follow-up of an earlier one in the same
# session?" — generous enough to span a real conversation but tight enough
# that an unrelated later question doesn't get tagged as a follow-up.
_FOLLOWUP_WINDOW_S = 30 * 60

# Heuristic markers for questions that *look* like they need prior context
# even with no DB-side evidence (very short, leading discourse marker, or
# trailing "då"/"dock").
_LEADING_MARKER_RE = (
    "ok",
    "okej",
    "ja",
    "nej",
    "men",
    "och",
    "då",
    "tack",
    "fortsätt",
)


def _parse_since(since: str | None) -> int:
    """Same convention as scripts/stats.py: '7d', '24h', or ISO date."""
    if not since:
        return 0
    if since.endswith(("d", "h")):
        unit = since[-1]
        n = int(since[:-1])
        secs = n * (86400 if unit == "d" else 3600)
        return int(time.time() - secs)
    return int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp())


def _select_rows(
    db_path: str,
    since_ts: int,
    *,
    want_gate_fail: bool,
    want_negative: bool,
    low_conf: float | None,
    limit: int,
) -> list[dict]:
    """Return qa_log rows matching the requested failure signal(s).

    For each row we also attach `has_prior_in_session`: 1 if there is an
    earlier qa_log row from the same channel/session within the
    follow-up window (so the row is likely a continuation that depends
    on the previous turn for meaning).
    """
    where = ["q.ts >= ?"]
    params: list[object] = [since_ts]
    or_clauses: list[str] = []

    if want_gate_fail:
        or_clauses.append("q.gate_pass = 0")
    if low_conf is not None:
        or_clauses.append("(q.gate_pass = 1 AND q.rerank_top1 < ?)")
        params.append(low_conf)
    if want_negative:
        or_clauses.append(
            "EXISTS (SELECT 1 FROM feedback f WHERE f.qa_id = q.id AND f.sentiment = 'negative')"
        )

    if or_clauses:
        where.append("(" + " OR ".join(or_clauses) + ")")

    # `prior` matches an earlier row in the same conversation: same
    # channel + (root_id OR channel_id), within the follow-up window.
    # For web rows channel_id is the session_id; for MM rows root_id is
    # the thread parent.
    sql = (
        "SELECT q.id, q.ts, q.lang, q.question, q.gate_pass, q.gate_reason, "
        "q.rerank_top1, q.rerank_meanK, q.distinct_sources, q.retrieved_chunk_ids, "
        "q.channel_type, q.channel_id, q.root_id, "
        "(SELECT COUNT(*) FROM feedback f WHERE f.qa_id = q.id AND f.sentiment = 'negative') "
        "  AS neg_count, "
        "(SELECT 1 FROM qa_log p WHERE p.id <> q.id AND p.ts < q.ts AND p.ts >= q.ts - ? "
        "   AND p.channel_type = q.channel_type "
        "   AND ((p.root_id IS NOT NULL AND p.root_id = q.root_id) "
        "        OR (q.channel_type = 'W' AND p.channel_id = q.channel_id)) "
        "   LIMIT 1) AS has_prior_in_session "
        "FROM qa_log q WHERE " + " AND ".join(where) + " ORDER BY q.ts DESC LIMIT ?"
    )
    params.insert(0, _FOLLOWUP_WINDOW_S)
    params.append(limit)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _looks_context_dependent(question: str) -> bool:
    """Text-only heuristic: short discourse-marker / pronoun questions that
    can't stand alone even without a session-prior signal."""
    import re as _re

    q = (question or "").strip().lower()
    if not q:
        return False
    words = q.split()
    # Bare cohort answer to a "which year?" clarification (e.g. "HT2025").
    if _re.fullmatch(r"(ht|vt)\s*\d{4}\??", q):
        return True
    # Bare program code possibly with a discourse marker ("CFATE då", "CFATE").
    if len(words) <= 3 and _re.match(r"^[a-z]{5}( då| ?\??)?$", q):
        return True
    if len(words) <= 4 and any(q.startswith(m + " ") or q == m for m in _LEADING_MARKER_RE):
        return True
    if len(words) <= 5 and (q.endswith(" då") or q.endswith(" då?")):
        return True
    # Bare-pronoun start without an explicit subject ("Är alla …", "Vilka är …",
    # "Vilket är …"). Cap word-count so a long question still gets retrieved
    # on its own merits.
    if len(words) <= 8 and any(
        q.startswith(p + " ")
        for p in (
            "är alla",
            "är de",
            "är det",
            "är dessa",
            "vilka är de",
            "vilka är dessa",
            "vilka är valbara",
            "vilket är de",
            "vad är de",
        )
    ):
        return True
    return False


def _load_ignore(cfg) -> dict[int, dict]:
    """Read `data/replay_ignore.yaml` if present. Returns {id: entry}."""
    path = cfg.absolute(Path("data/replay_ignore.yaml"))
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[int, dict] = {}
    for entry in raw.get("ignored", []) or []:
        if not isinstance(entry, dict):
            continue
        try:
            qa_id = int(entry["id"])
        except (KeyError, TypeError, ValueError):
            continue
        out[qa_id] = entry
    return out


def _classify(
    before: dict,
    now_passed: bool,
    now_top1: float,
    *,
    is_ignored: bool,
    needs_context: bool,
) -> str:
    if is_ignored:
        return "IGNORED"
    if needs_context:
        return "NEEDS-CONTEXT"
    was_passed = bool(before["gate_pass"])
    was_top1 = float(before["rerank_top1"])
    delta = now_top1 - was_top1
    if not was_passed and now_passed:
        return "FIXED"
    if was_passed and not now_passed:
        return "REGRESSED"
    if not was_passed and not now_passed:
        return "STILL-REFUSE"
    if delta >= 0.5:
        return "IMPROVED"
    if delta <= -0.5:
        return "REGRESSED"
    return "UNCHANGED"


_OUTCOME_STYLE = {
    "FIXED": "green",
    "IMPROVED": "green",
    "UNCHANGED": "dim",
    "NEEDS-CONTEXT": "cyan",
    "IGNORED": "magenta",
    "STILL-REFUSE": "yellow",
    "REGRESSED": "red",
}


@click.command()
@click.option("--since", default="30d", help="Limit to last 30d / 24h / ISO date.")
@click.option("--negative-feedback", is_flag=True, help="Include rows with 👎 feedback.")
@click.option("--gate-fail", is_flag=True, help="Include refusals (gate_pass=0).")
@click.option(
    "--low-confidence",
    type=float,
    default=None,
    help="Include passes with rerank_top1 < THRESH (e.g. 1.0).",
)
@click.option(
    "--with-llm",
    is_flag=True,
    help="Run the full pipeline including LLM generation (slow; default is retrieval+gate only).",
)
@click.option("--limit", default=50, help="Max rows to replay.")
@click.option("--show-sources", is_flag=True, help="Print the new top-3 sources per row.")
def main(
    since: str,
    negative_feedback: bool,
    gate_fail: bool,
    low_confidence: float | None,
    with_llm: bool,
    limit: int,
    show_sources: bool,
):
    cfg = get_config()
    console = Console()

    # Default selection mirrors the manual SQL we tend to run when looking
    # for "what failed lately": gate refusal OR low-confidence pass OR 👎.
    if not (gate_fail or negative_feedback or low_confidence is not None):
        gate_fail = True
        negative_feedback = True
        low_confidence = 1.0
        console.print(
            "[dim]No filter flags set — defaulting to "
            "gate-fail OR low-confidence(<1.0) OR negative-feedback.[/dim]"
        )

    since_ts = _parse_since(since)
    db_path = str(cfg.absolute(cfg.paths.logs_db))
    rows = _select_rows(
        db_path,
        since_ts,
        want_gate_fail=gate_fail,
        want_negative=negative_feedback,
        low_conf=low_confidence,
        limit=limit,
    )
    if not rows:
        console.print(f"[yellow]No matching qa_log rows since {since}.[/yellow]")
        return

    ignored = _load_ignore(cfg)
    console.print(
        f"Replaying [bold]{len(rows)}[/bold] rows since {since} "
        f"({'with LLM' if with_llm else 'retrieval+gate only'}, "
        f"{len(ignored)} ignored by data/replay_ignore.yaml)…\n"
    )

    tally: dict[str, int] = {}
    needs_context_suggestions: list[tuple[int, str]] = []
    for r in rows:
        question = r["question"] or ""
        is_ignored = r["id"] in ignored
        # Auto-detect "this row is a follow-up" if there was an earlier
        # turn in the same session, OR the text alone reads like one.
        needs_context = False
        if not is_ignored:
            # Require BOTH signals: the SQL prior-turn check alone over-flags
            # because plenty of follow-up questions are fully self-contained
            # ("Finns det ett program som heter CFUSK?"), and the text
            # heuristic alone misses ones whose context dependency isn't
            # obvious from words ("HT2025"). The intersection is sharper.
            looks_dep = _looks_context_dependent(question)
            has_prior = bool(r.get("has_prior_in_session"))
            needs_context = has_prior and looks_dep

        if needs_context or is_ignored:
            # Skip the actual replay for these — they're not real corpus
            # signals and re-running just burns CPU on the rerank model.
            now_passed = bool(r["gate_pass"])
            now_top1 = float(r["rerank_top1"])
            now_meanK = float(r["rerank_meanK"])
            now_chunks: list = []
        elif with_llm:
            res = run_answer(question, cfg=cfg)
            now_passed = res.gate.passed
            now_top1 = res.gate.top1
            now_meanK = res.gate.meanK
            now_chunks = res.retrieval.reranked
        else:
            ret = retrieve(cfg, question)
            gate = evaluate_gate(cfg, ret)
            now_passed = gate.passed
            now_top1 = gate.top1
            now_meanK = gate.meanK
            now_chunks = ret.reranked

        outcome = _classify(
            r, now_passed, now_top1, is_ignored=is_ignored, needs_context=needs_context
        )
        tally[outcome] = tally.get(outcome, 0) + 1
        if outcome == "NEEDS-CONTEXT":
            needs_context_suggestions.append((r["id"], question[:80]))

        before_label = "pass" if r["gate_pass"] else f"refuse ({r['gate_reason']})"
        now_label = "pass" if now_passed else "refuse"
        neg_marker = " 👎" if r["neg_count"] else ""
        style = _OUTCOME_STYLE.get(outcome, "white")
        ts = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M")
        q_short = (question[:90] + "…") if len(question) > 90 else question

        console.print(
            f"[{style}]{outcome:<13}[/{style}] "
            f"qa_id={r['id']:<5} {ts}{neg_marker}  "
            f"[dim]{r['lang']}[/dim]  {q_short!r}"
        )
        if outcome == "IGNORED":
            note = ignored[r["id"]].get("note") or ignored[r["id"]].get("reason", "")
            console.print(f"   [dim]ignored: {note}[/dim]")
        elif outcome == "NEEDS-CONTEXT":
            console.print(
                "   [dim]skipped — looks like a follow-up "
                "(prior turn in same session, or short discourse-marker question)[/dim]"
            )
        else:
            console.print(
                f"   was: {before_label} top1={r['rerank_top1']:+.2f}  "
                f"→ now: {now_label} top1={now_top1:+.2f} meanK={now_meanK:+.2f}"
            )
            if show_sources and now_chunks:
                for c in now_chunks[:3]:
                    src = c.source_url or c.rel_source
                    console.print(f"   • {src}  [dim]({c.rerank_score:+.2f})[/dim]")

    # Summary table.
    table = Table(title="Replay summary", show_header=True)
    table.add_column("outcome")
    table.add_column("count", justify="right")
    for k in (
        "FIXED",
        "IMPROVED",
        "UNCHANGED",
        "STILL-REFUSE",
        "REGRESSED",
        "NEEDS-CONTEXT",
        "IGNORED",
    ):
        if k in tally:
            style = _OUTCOME_STYLE.get(k, "white")
            table.add_row(f"[{style}]{k}[/{style}]", str(tally[k]))
    console.print()
    console.print(table)

    if needs_context_suggestions:
        console.print(
            "\n[dim]Auto-detected as needing prior-turn context. To make this "
            "permanent, append to `data/replay_ignore.yaml`:[/dim]"
        )
        for qa_id, q in needs_context_suggestions:
            console.print(f"  - {{ id: {qa_id}, reason: needs-context, note: {q!r} }}")


if __name__ == "__main__":
    main()
