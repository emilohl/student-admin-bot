"""Set the bot's display name, description, and user-record name fields.

Run once per deploy (or after rotating the bot token):

    uv run student-bot-mm-profile

Patches both halves of a Mattermost bot identity:
- the *bot record* (sidebar tooltip / popover) via PATCH /bots/{id}
- the *user record* (mention popover, channel header) via PATCH /users/me
"""

from __future__ import annotations

import logging
import sys

import click

from student_bot.bot.mattermost_client import _SslShim  # noqa: F401  (apply SSL shim)
from student_bot.config import get_config

from mattermostdriver import Driver


DEFAULT_DISPLAY_NAME = "Lux Adminbot"
DEFAULT_FIRST_NAME = "Lux"
DEFAULT_LAST_NAME = "Adminbot"
DEFAULT_NICKNAME = "Lux Adminbot"
DEFAULT_DESCRIPTION_SV = (
    "Automatisk assistent för administrativa frågor om CTFYS-programmet vid KTH. "
    "Svaren baseras på indexerade kursdokument — kontrollera alltid mot källorna "
    "och kontakta studievägledaren för personliga ärenden."
)


@click.command()
@click.option("--display-name", default=DEFAULT_DISPLAY_NAME, show_default=True)
@click.option("--first-name", default=DEFAULT_FIRST_NAME, show_default=True)
@click.option("--last-name", default=DEFAULT_LAST_NAME, show_default=True)
@click.option("--nickname", default=DEFAULT_NICKNAME, show_default=True)
@click.option(
    "--description",
    default=DEFAULT_DESCRIPTION_SV,
    show_default=False,
    help="Bot description (Swedish by default).",
)
@click.option("--dry-run", is_flag=True, help="Print what would change, don't call MM.")
def main(
    display_name: str,
    first_name: str,
    last_name: str,
    nickname: str,
    description: str,
    dry_run: bool,
):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("mm_profile")

    cfg = get_config()
    if not cfg.mattermost_secrets:
        raise click.ClickException("Mattermost credentials not set (see .env.example)")

    s = cfg.mattermost_secrets
    driver = Driver(
        {
            "url": s.url,
            "token": s.token,
            "scheme": s.scheme,
            "port": s.port,
            "basepath": "/api/v4",
            "verify": True,
            "timeout": 30,
            "debug": False,
        }
    )
    driver.login()
    me = driver.users.get_user(user_id="me")
    bot_id = me["id"]
    log.info("logged in as %s (%s)", me.get("username"), bot_id)

    bot_patch = {"display_name": display_name, "description": description}
    user_patch = {
        "first_name": first_name,
        "last_name": last_name,
        "nickname": nickname,
    }

    if dry_run:
        log.info("DRY RUN — would PATCH /bots/%s with %s", bot_id, bot_patch)
        log.info("DRY RUN — would PATCH /users/me with %s", user_patch)
        sys.exit(0)

    driver.bots.patch_bot(bot_id, bot_patch)
    log.info("patched bot record: %s", bot_patch)

    driver.users.patch_user("me", user_patch)
    log.info("patched user record: %s", user_patch)

    log.info("done.")


if __name__ == "__main__":
    main()
