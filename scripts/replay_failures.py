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

import click
from rich.console import Console
from rich.table import Table

from student_bot.bot.gate import evaluate as evaluate_gate
from student_bot.bot.pipeline import answer as run_answer
from student_bot.bot.retrieval import retrieve
from student_bot.config import get_config


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
    """Return qa_log rows matching the requested failure signal(s)."""
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

    sql = (
        "SELECT q.id, q.ts, q.lang, q.question, q.gate_pass, q.gate_reason, "
        "q.rerank_top1, q.rerank_meanK, q.distinct_sources, q.retrieved_chunk_ids, "
        "(SELECT COUNT(*) FROM feedback f WHERE f.qa_id = q.id AND f.sentiment = 'negative') "
        "  AS neg_count "
        "FROM qa_log q WHERE " + " AND ".join(where) + " ORDER BY q.ts DESC LIMIT ?"
    )
    params.append(limit)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _classify(before: dict, now_passed: bool, now_top1: float) -> str:
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

    console.print(
        f"Replaying [bold]{len(rows)}[/bold] rows since {since} "
        f"({'with LLM' if with_llm else 'retrieval+gate only'})…\n"
    )

    tally: dict[str, int] = {}
    for r in rows:
        question = r["question"] or ""
        if with_llm:
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

        outcome = _classify(r, now_passed, now_top1)
        tally[outcome] = tally.get(outcome, 0) + 1

        before_label = "pass" if r["gate_pass"] else f"refuse ({r['gate_reason']})"
        now_label = "pass" if now_passed else "refuse"
        neg_marker = " 👎" if r["neg_count"] else ""
        style = _OUTCOME_STYLE.get(outcome, "white")
        ts = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M")
        q_short = (question[:90] + "…") if len(question) > 90 else question

        console.print(
            f"[{style}]{outcome:<12}[/{style}] "
            f"qa_id={r['id']:<5} {ts}{neg_marker}  "
            f"[dim]{r['lang']}[/dim]  {q_short!r}"
        )
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
    for k in ("FIXED", "IMPROVED", "UNCHANGED", "STILL-REFUSE", "REGRESSED"):
        if k in tally:
            style = _OUTCOME_STYLE.get(k, "white")
            table.add_row(f"[{style}]{k}[/{style}]", str(tally[k]))
    console.print()
    console.print(table)


if __name__ == "__main__":
    main()
