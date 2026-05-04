"""student-bot-jargon — admin CLI for the jargon dictionary.

Curates `dictionary.json` (canonical) and reviews `dictionary_proposals.json`
(student-submitted queue). Uses the same JSON shape as the runtime so a
careful admin can also edit the file by hand.
"""
from __future__ import annotations

import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from student_bot.config import Config, get_config
from student_bot.jargon import (
    Jargon,
    JargonEntry,
    _nfc_lower,
    _read_json,
    _write_json,
)


def _proposals_path(cfg: Config) -> Path:
    return cfg.absolute(Path(cfg.jargon.proposals_file))


def _load_proposals(cfg: Config) -> dict:
    path = _proposals_path(cfg)
    if not path.exists():
        return {"version": 1, "entries": {}}
    return _read_json(path)


def _save_proposals(cfg: Config, data: dict) -> None:
    _write_json(_proposals_path(cfg), data)


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


@click.group()
def main():
    """Manage the student-bot jargon dictionary."""


@main.command("list")
def list_cmd():
    """Show all canonical entries."""
    cfg = get_config()
    j = Jargon.from_config(cfg)
    console = Console()
    entries = j.all_entries()
    if not entries:
        console.print("[dim]dictionary is empty[/dim]")
        return
    t = Table(title=f"{len(entries)} entries — {cfg.absolute(Path(cfg.jargon.file))}")
    t.add_column("term", style="bold")
    t.add_column("expansion")
    t.add_column("definition")
    t.add_column("lang")
    t.add_column("added_by")
    for e in entries:
        t.add_row(e.term, e.expansion, (e.definition or "—"), e.lang, e.added_by or "—")
    console.print(t)


@main.command("add")
@click.argument("term")
@click.argument("expansion")
@click.option("--lang", default="sv", help="sv | en | any")
@click.option("--def", "definition", default="", help="One-line definition.")
@click.option("--by", default="admin")
def add_cmd(term: str, expansion: str, lang: str, definition: str, by: str):
    """Add or update an entry."""
    cfg = get_config()
    j = Jargon.from_config(cfg)
    e = JargonEntry(
        key=_nfc_lower(term),
        term=term,
        expansion=expansion,
        lang=lang,
        definition=definition,
        added_by=by,
        added_ts=_today(),
    )
    j.add_entry(e)
    click.echo(f"added/updated {term} → {expansion}")


@main.command("remove")
@click.argument("term")
def remove_cmd(term: str):
    """Remove an entry by term or key."""
    cfg = get_config()
    j = Jargon.from_config(cfg)
    if j.remove_entry(term):
        click.echo(f"removed {term}")
    else:
        raise click.ClickException(f"no entry named {term!r}")


@main.command("proposals")
def proposals_cmd():
    """List pending proposals from `dictionary_proposals.json`."""
    cfg = get_config()
    console = Console()
    data = _load_proposals(cfg)
    entries = data.get("entries", {})
    pending = [(k, v) for k, v in entries.items() if v.get("status") == "pending"]
    if not pending:
        console.print("[dim]no pending proposals[/dim]")
        return
    t = Table(title=f"{len(pending)} pending — {_proposals_path(cfg)}")
    t.add_column("#", justify="right")
    t.add_column("term", style="bold")
    t.add_column("expansion")
    t.add_column("definition")
    t.add_column("lang")
    t.add_column("when")
    for i, (_, v) in enumerate(pending, 1):
        ts = v.get("suggested_ts") or 0
        when = time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "—"
        t.add_row(str(i), v.get("term", ""), v.get("expansion", ""),
                   v.get("definition", "") or "—", v.get("lang", ""), when)
    console.print(t)


def _resolve_proposal_index(cfg: Config, n: int) -> tuple[str, dict]:
    data = _load_proposals(cfg)
    entries = data.get("entries", {})
    pending = [(k, v) for k, v in entries.items() if v.get("status") == "pending"]
    if n < 1 or n > len(pending):
        raise click.ClickException(f"no proposal #{n} (got {len(pending)} pending)")
    return pending[n - 1]


@main.command("accept")
@click.argument("n", type=int)
@click.option("--def", "definition", default=None,
              help="Override the suggested definition.")
@click.option("--lang", default=None, help="Override the suggested language.")
def accept_cmd(n: int, definition: str | None, lang: str | None):
    """Move proposal #N from the queue into the canonical dictionary."""
    cfg = get_config()
    key, body = _resolve_proposal_index(cfg, n)

    j = Jargon.from_config(cfg)
    entry = JargonEntry(
        key=key,
        term=body.get("term", key),
        expansion=body.get("expansion", ""),
        lang=lang or body.get("lang", "sv"),
        definition=definition if definition is not None else body.get("definition", ""),
        added_by="admin (accepted from student suggestion)",
        added_ts=_today(),
    )
    j.add_entry(entry)

    data = _load_proposals(cfg)
    data["entries"][key]["status"] = "accepted"
    data["entries"][key]["resolved_ts"] = int(time.time())
    _save_proposals(cfg, data)
    click.echo(f"accepted: {entry.term} → {entry.expansion}")


@main.command("reject")
@click.argument("n", type=int)
@click.option("--reason", default="")
def reject_cmd(n: int, reason: str):
    """Mark proposal #N as rejected (kept for audit)."""
    cfg = get_config()
    key, _ = _resolve_proposal_index(cfg, n)
    data = _load_proposals(cfg)
    data["entries"][key]["status"] = "rejected"
    data["entries"][key]["resolved_ts"] = int(time.time())
    if reason:
        data["entries"][key]["rejection_reason"] = reason
    _save_proposals(cfg, data)
    click.echo(f"rejected proposal #{n}")


if __name__ == "__main__":
    main()
