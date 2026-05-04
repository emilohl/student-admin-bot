"""student-bot-stats: per-topic question counts, latency, and 👍/👎 ratios.

Reads SQLite logs only — does not call the LLM.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import click
from rich.console import Console
from rich.table import Table

from student_bot.bot.topics import load_topics
from student_bot.config import get_config
from student_bot.logging_db import LogDB


def _parse_since(since: str | None) -> int:
    if not since:
        return 0
    if since.endswith(("d", "h")):
        unit = since[-1]
        n = int(since[:-1])
        secs = n * (86400 if unit == "d" else 3600)
        return int(time.time() - secs)
    # ISO date.
    return int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp())


@click.command()
@click.option("--since", default=None, help="Limit to last 7d / 24h / ISO date.")
def main(since: str | None):
    cfg = get_config()
    db = LogDB(cfg)
    console = Console()

    since_ts = _parse_since(since)
    overall = db.overall_counts(since_ts)
    by_topic = db.stats_by_topic(since_ts)

    topics = {t.id: t for t in load_topics(cfg)}

    range_label = f"since {since}" if since else "all time"
    head = Table(title=f"Overall ({range_label})", show_header=False)
    head.add_column("metric", style="bold")
    head.add_column("value", justify="right")
    head.add_row("logged questions", str(overall["logged"]))
    head.add_row("answered (gate pass)", str(overall["answered"]))
    head.add_row("avg latency (ms)", str(overall["avg_latency_ms"]))
    head.add_row("anonymous-only counter", str(overall["anon"]))
    console.print(head)

    if not by_topic:
        console.print("[dim]no logged questions yet[/dim]")
        return

    body = Table(title=f"By topic ({range_label})")
    body.add_column("topic")
    body.add_column("label")
    body.add_column("n", justify="right")
    body.add_column("answered", justify="right")
    body.add_column("avg ms", justify="right")
    body.add_column("👍", justify="right", style="green")
    body.add_column("👎", justify="right", style="red")
    body.add_column("👍 ratio", justify="right")

    for row in by_topic:
        tid = row["topic"]
        label = topics.get(tid).sv if tid in topics else tid
        ratio = ""
        denom = row["thumbs_up"] + row["thumbs_down"]
        if denom:
            ratio = f"{(row['thumbs_up'] / denom) * 100:.0f}%"
        body.add_row(
            tid,
            label,
            str(row["n"]),
            str(row["answered"]),
            str(row["avg_latency_ms"]),
            str(row["thumbs_up"]),
            str(row["thumbs_down"]),
            ratio,
        )
    console.print(body)


if __name__ == "__main__":
    main()
