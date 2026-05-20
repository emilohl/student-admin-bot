"""Add or update a user in the web_users password file.

Format: <username>:scrypt:<salt_b64>:<hash_b64>[:admin]

The optional 4th `admin` field grants the diagnostic surface in the web
UI (per-stage timing histograms, RSS trend, the ability to inspect other
users' debug payloads). Without it, the user only sees their own debug
data when they opt into "Learn more about how this chatbot works".
"""

from __future__ import annotations

import getpass
from pathlib import Path

import click

from student_bot.config import get_config
from student_bot.web.auth import UserRecord, hash_password, load_users_file


@click.command()
@click.argument("username")
@click.option("--password", default=None, help="Password (prompts if omitted).")
@click.option(
    "--admin/--no-admin",
    "admin",
    default=None,
    help=(
        "Grant or revoke admin (diagnostic surface). Default: preserve current "
        "value for an existing user, or non-admin for a new one."
    ),
)
def main(username: str, password: str | None, admin: bool | None):
    cfg = get_config()
    users_path = cfg.absolute(Path(cfg.web.users_file))
    users_path.parent.mkdir(parents=True, exist_ok=True)

    if password is None:
        password = getpass.getpass(f"Password for {username}: ")
        confirm = getpass.getpass("Confirm: ")
        if password != confirm:
            raise click.ClickException("passwords do not match")
    if not password:
        raise click.ClickException("password is empty")

    users = load_users_file(users_path)
    existing = users.get(username)
    # Resolve final admin flag: explicit CLI value wins; otherwise preserve
    # the existing user's flag; otherwise default to non-admin.
    if admin is None:
        is_admin = existing.is_admin if existing else False
    else:
        is_admin = admin

    users[username] = UserRecord(record=hash_password(password), is_admin=is_admin)

    lines = [
        "# student-bot web users — managed by `student-bot-mkuser`",
        "# format: <username>:scrypt:<salt_b64>:<hash_b64>[:admin]",
    ]
    for name, info in sorted(users.items()):
        suffix = ":admin" if info.is_admin else ""
        lines.append(f"{name}:{info.record}{suffix}")
    users_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    admin_note = " (admin)" if is_admin else ""
    click.echo(
        f"wrote {users_path}  ({len(users)} user{'s' if len(users) != 1 else ''}); "
        f"{username}{admin_note}"
    )


if __name__ == "__main__":
    main()
