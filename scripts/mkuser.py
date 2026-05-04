"""Add or update a user in the web_users password file.

Format: <username>:scrypt:<salt_b64>:<hash_b64>
"""

from __future__ import annotations

import getpass
from pathlib import Path

import click

from student_bot.config import get_config
from student_bot.web.auth import hash_password, load_users_file


@click.command()
@click.argument("username")
@click.option("--password", default=None, help="Password (prompts if omitted).")
def main(username: str, password: str | None):
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
    users[username] = hash_password(password)

    lines = [
        "# student-bot web users — managed by `student-bot-mkuser`",
        "# format: <username>:scrypt:<salt_b64>:<hash_b64>",
    ]
    for name, record in sorted(users.items()):
        lines.append(f"{name}:{record}")
    users_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    click.echo(f"wrote {users_path}  ({len(users)} user{'s' if len(users) != 1 else ''})")


if __name__ == "__main__":
    main()
