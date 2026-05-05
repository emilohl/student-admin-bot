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
    "Svaren baseras på indexerade FAQ- och styrdokument — kontrollera alltid mot källorna "
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
@click.option(
    "--repair-bot",
    is_flag=True,
    help=(
        "If the user has is_bot=true but no bots-table row (orphaned bot), "
        "POST /users/{id}/convert_to_bot to recreate the bot record. "
        "Requires the calling token to belong to a system admin."
    ),
)
def main(
    display_name: str,
    first_name: str,
    last_name: str,
    nickname: str,
    description: str,
    dry_run: bool,
    repair_bot: bool,
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
    is_bot_account = bool(me.get("is_bot"))
    log.info(
        "logged in as %s (%s) — %s account",
        me.get("username"),
        bot_id,
        "bot" if is_bot_account else "user",
    )

    bot_patch = {"display_name": display_name, "description": description}
    user_patch = {
        "first_name": first_name,
        "last_name": last_name,
        "nickname": nickname,
    }

    if dry_run:
        if is_bot_account:
            log.info("DRY RUN — would PATCH /bots/%s with %s", bot_id, bot_patch)
        else:
            log.info(
                "DRY RUN — account is a regular user (not a registered bot account); "
                "skipping PATCH /bots/* (display_name/description are bot-only fields)"
            )
        log.info("DRY RUN — would PATCH /users/me with %s", user_patch)
        sys.exit(0)

    # mattermostdriver 7.3.2 registers `Bots` in its endpoint dict but
    # forgot to expose it as a `@property` like users/posts/etc., so we
    # reach through `_api` directly. Same object either way.
    bots_api = driver._api["bots"]

    def _patch_bot():
        bots_api.patch_bot(bot_id, bot_patch)
        log.info("patched bot record: %s", bot_patch)

    if is_bot_account:
        try:
            _patch_bot()
        except Exception as e:
            # Orphaned bot: `is_bot=true` on the user, but no row in the
            # `bots` table. `convert_to_bot` recreates the missing row,
            # but requires a system-admin token.
            msg = str(e).lower()
            is_404 = "bot does not exist" in msg or "not found" in msg
            if is_404 and repair_bot:
                log.info("bot row missing; calling /users/%s/convert_to_bot", bot_id)
                try:
                    driver.client.post(f"/users/{bot_id}/convert_to_bot")
                    log.info("convert_to_bot succeeded; retrying patch")
                    _patch_bot()
                except Exception as e2:
                    log.warning(
                        "could not repair orphaned bot row: %s. The token "
                        "needs system-admin permissions to call "
                        "/users/{id}/convert_to_bot. Falling back to "
                        "user-record patch only.",
                        e2,
                    )
            elif is_404:
                log.warning(
                    "PATCH /bots/%s returned 404 — orphaned bot (is_bot=true on "
                    "the user, but no row in the bots table). Re-run with "
                    "`--repair-bot` to recreate it via convert_to_bot. Requires "
                    "system-admin permissions on the calling token.",
                    bot_id,
                )
            else:
                log.warning("patch_bot failed: %s. Falling back to user patch.", e)
    else:
        log.warning(
            "account is a regular user (is_bot=false) — skipping bot-record "
            "patch (display_name/description). To set those, register a real "
            "bot account via System Console -> Integrations -> Bot Accounts."
        )

    driver.users.patch_user("me", user_patch)
    log.info("patched user record: %s", user_patch)

    log.info("done.")


if __name__ == "__main__":
    main()
