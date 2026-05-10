"""One-off backfill for qa_log.prompt_tokens / qa_log.gen_tokens.

For rows logged before the token columns existed, estimate prompt_tokens from
len(question)/4 and gen_tokens from len(answer)/4 — the same coarse heuristic
that pipeline.py uses for live UI telemetry. Idempotent: only updates rows
where the columns are still NULL.

Run once after deploying the migration:

    uv run python -m scripts.backfill_tokens
"""

from __future__ import annotations

import sqlite3

import click

from student_bot.config import get_config


@click.command()
@click.option("--dry-run", is_flag=True, help="Show how many rows would be updated, then exit.")
def main(dry_run: bool):
    cfg = get_config()
    db_path = cfg.absolute(cfg.paths.logs_db)
    if not db_path.exists():
        click.echo(f"no log DB at {db_path}")
        return

    with sqlite3.connect(str(db_path)) as conn:
        n_null = conn.execute(
            "SELECT COUNT(*) FROM qa_log WHERE prompt_tokens IS NULL OR gen_tokens IS NULL"
        ).fetchone()[0]
        click.echo(f"{n_null} rows with NULL token columns")
        if dry_run or n_null == 0:
            return
        # max(1, ...) so a one-character question still counts as one prompt token.
        conn.execute(
            """
            UPDATE qa_log
               SET prompt_tokens = MAX(1, length(question) / 4),
                   gen_tokens    = MAX(0, length(answer) / 4)
             WHERE prompt_tokens IS NULL OR gen_tokens IS NULL
            """
        )
        conn.commit()
        click.echo(f"updated {n_null} rows")


if __name__ == "__main__":
    main()
