"""Mattermost websocket bot.

Behaviour:
- Triggers on every DM post AND any channel post containing the configured mention.
- Filters out the bot's own posts and other bots.
- Replies in-thread (uses post.root_id when set, else post.id).
- On a user's first DM ever, posts a one-line GDPR notice before answering.
- While the RAG pipeline runs, reacts with :thinking: on the user's post as
  a lightweight "bot is working" indicator (removed when the reply lands).
- Listens for 👍 / 👎 reactions on bot posts and records sentiment.
- Wraps the websocket loop in a reconnect-with-backoff retry loop.
- LLM calls run on a worker thread so websocket events keep flowing.
"""

from __future__ import annotations

import json
import logging
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mattermostdriver import Driver
from rich.logging import RichHandler

from student_bot.bot.pipeline import answer
from student_bot.config import Config, get_config
from student_bot.logging_db import LogDB


# --- Python 3.12+ compatibility shim for mattermostdriver 7.3.2 ----------
# That version constructs the websocket SSL context with
# `ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)`, which
# yields a PROTOCOL_TLS_SERVER context — Python 3.12+ refuses to use a
# server-purpose context on a client socket and the connect loop fails
# with "Cannot create a client socket with a PROTOCOL_TLS_SERVER context".
# Upstream issue (still open):
#   https://github.com/Vaelor/python-mattermost-driver/issues/115
# We replace the `ssl` reference inside that one module with a thin shim
# that forces `Purpose.SERVER_AUTH`, leaving the rest of the process's
# ssl module untouched.
import ssl as _ssl_module
import mattermostdriver.websocket as _mm_ws


class _SslShim:
    Purpose = _ssl_module.Purpose
    CERT_NONE = _ssl_module.CERT_NONE

    @staticmethod
    def create_default_context(purpose=None, **kwargs):
        return _ssl_module.create_default_context(
            purpose=_ssl_module.Purpose.SERVER_AUTH,
            **kwargs,
        )


_mm_ws.ssl = _SslShim
# -------------------------------------------------------------------------


log = logging.getLogger("student_bot")


GDPR_NOTICE_SV = (
    "Hej! Jag är en automatisk assistent för administrativa frågor om CTFYS. "
    "Frågor och feedback (👍 / 👎) loggas anonymt för att förbättra boten. "
    "Skriv `!privacy off` om du vill stänga av loggning av dina frågor "
    "(`!privacy on` slår på igen, `!privacy status` visar nuläget). "
    "Reagera gärna med 👍 eller 👎 på mina svar."
)
GDPR_NOTICE_EN = (
    "Hi! I'm an automated assistant for administrative questions about CTFYS. "
    "Questions and feedback (👍 / 👎) are logged anonymously to help improve the bot. "
    "Send `!privacy off` to stop logging your questions "
    "(`!privacy on` re-enables it, `!privacy status` shows current state). "
    "Please react with 👍 or 👎 on my replies."
)

PRIVACY_OFF_SV = "Loggning är nu avstängd. Innehållet i dina frågor lagras inte längre."
PRIVACY_OFF_EN = "Logging disabled. Your question content is no longer stored."
PRIVACY_ON_SV = "Loggning är på. Frågor och svar lagras anonymt."
PRIVACY_ON_EN = "Logging enabled. Questions and answers are stored anonymously."
PRIVACY_STATUS_SV = "Loggning för dig är just nu: {state}."
PRIVACY_STATUS_EN = "Logging for you is currently: {state}."

POSITIVE_EMOJI = {"+1", "thumbsup", "white_check_mark"}
NEGATIVE_EMOJI = {"-1", "thumbsdown", "x", "no_entry_sign"}

# Emoji used as a "bot is thinking" indicator on the user's post while the
# RAG pipeline runs. Added when work starts, removed when the reply lands.
THINKING_EMOJI = "thinking"


@dataclass
class _Job:
    user_id: str
    channel_id: str
    channel_type: str
    root_id: str
    user_post_id: str
    question: str


class StudentBot:
    def __init__(self, cfg: Config):
        if not cfg.mattermost_secrets:
            raise RuntimeError("Mattermost credentials not set (see .env.example)")
        if not cfg.user_id_hash_salt:
            raise RuntimeError("USER_ID_HASH_SALT not set (see .env.example)")
        self.cfg = cfg
        self.db = LogDB(cfg)
        self.queue: queue.Queue[_Job] = queue.Queue()
        self.shutdown = threading.Event()
        self._driver_lock = threading.Lock()
        self.bot_user_id: str | None = None
        self.driver: Driver | None = None

    # --- driver lifecycle ---

    def _make_driver(self) -> Driver:
        s = self.cfg.mattermost_secrets
        assert s is not None
        # `keepalive=True` makes the websocket loop auto-reconnect with
        # `keepalive_delay` seconds between attempts; we cap with our own
        # outer loop in case the whole thing crashes.
        return Driver(
            {
                "url": s.url,
                "token": s.token,
                "scheme": s.scheme,
                "port": s.port,
                "basepath": "/api/v4",
                "verify": True,
                "timeout": 30,
                "keepalive": True,
                "keepalive_delay": 5,
                "debug": False,
            }
        )

    def login(self) -> None:
        with self._driver_lock:
            self.driver = self._make_driver()
            self.driver.login()
            me = self.driver.users.get_user(user_id="me")
            self.bot_user_id = me["id"]
            log.info("logged in as %s (%s)", me.get("username"), self.bot_user_id)

    # --- worker thread: runs RAG, posts reply ---

    def worker_loop(self):
        while not self.shutdown.is_set():
            try:
                job = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle_job(job)
            except Exception:
                log.exception("worker failed on job")
            finally:
                self.queue.task_done()

    def _post(self, channel_id: str, message: str, root_id: str | None) -> str | None:
        assert self.driver is not None
        try:
            with self._driver_lock:
                resp = self.driver.posts.create_post(
                    {
                        "channel_id": channel_id,
                        "message": message,
                        "root_id": root_id or "",
                    }
                )
            return resp.get("id")
        except Exception:
            log.exception("post failed")
            return None

    def _react(self, post_id: str, emoji_name: str) -> None:
        """Add a reaction as the bot user. Failures are logged and swallowed —
        a deleted post or transient API error must not crash the worker."""
        if not post_id or not self.bot_user_id:
            return
        assert self.driver is not None
        try:
            with self._driver_lock:
                self.driver.reactions.create_reaction(
                    {
                        "user_id": self.bot_user_id,
                        "post_id": post_id,
                        "emoji_name": emoji_name,
                    }
                )
        except Exception:
            log.debug("create_reaction(%s) failed", emoji_name, exc_info=True)

    def _unreact(self, post_id: str, emoji_name: str) -> None:
        if not post_id or not self.bot_user_id:
            return
        assert self.driver is not None
        try:
            with self._driver_lock:
                self.driver.reactions.delete_reaction(self.bot_user_id, post_id, emoji_name)
        except Exception:
            log.debug("delete_reaction(%s) failed", emoji_name, exc_info=True)

    def _handle_jargon_command(self, job: _Job, body: str) -> bool:
        """Returns True if the message was handled as a !jargon command."""
        from student_bot.jargon import Jargon
        from student_bot.lang import detect

        text = body.strip()
        if not text.lower().startswith("!jargon"):
            return False
        rest = text[len("!jargon") :].strip()
        sub, _, tail = rest.partition(" ")
        sub = sub.lower()
        lang = detect(text) if text else "sv"

        jargon = Jargon.from_config(self.cfg)

        if sub in ("", "list"):
            entries = jargon.all_entries()
            if not entries:
                msg = "Ordlistan är tom." if lang == "sv" else "The dictionary is empty."
            else:
                head = (
                    "| Term | Betydelse | Förklaring |\n|---|---|---|"
                    if lang == "sv"
                    else "| Term | Means | Definition |\n|---|---|---|"
                )
                rows = [f"| {e.term} | {e.expansion} | {e.definition or '—'} |" for e in entries]
                msg = "\n".join([head, *rows])
            self._post(job.channel_id, msg, job.root_id)
            return True

        if sub == "suggest":
            term, sep, expansion = tail.partition("=")
            term = term.strip()
            expansion = expansion.strip()
            if not (term and sep and expansion):
                example = "`!jargon suggest KEX-jobb = kandidatexamensarbete`"
                err = f"Format: {example}" if lang == "sv" else f"Format: {example}"
                self._post(job.channel_id, err, job.root_id)
                return True
            self._record_proposal(job.user_id, term, expansion, lang)
            ack = (
                "Tack! Förslaget hamnar i kö för admin att granska."
                if lang == "sv"
                else "Thanks! Your suggestion is queued for admin review."
            )
            self._post(job.channel_id, ack, job.root_id)
            return True

        # Unknown subcommand — show help.
        help_sv = "Användning: `!jargon list` eller `!jargon suggest TERM = BETYDELSE`"
        help_en = "Usage: `!jargon list` or `!jargon suggest TERM = MEANING`"
        self._post(job.channel_id, help_en if lang == "en" else help_sv, job.root_id)
        return True

    def _record_proposal(self, user_id: str, term: str, expansion: str, lang: str) -> None:
        """Append a proposal to dictionary_proposals.json."""
        from student_bot.jargon import _nfc_lower, _read_json, _write_json

        path = self.cfg.absolute(Path(self.cfg.jargon.proposals_file))
        data = _read_json(path) if path.exists() else {"version": 1, "entries": {}}
        entries = data.setdefault("entries", {})
        key = _nfc_lower(term)
        # If a duplicate is suggested, just update the timestamp; don't dedupe
        # the suggester because we want to know how many people asked.
        entries[key] = {
            "term": term,
            "expansion": expansion,
            "lang": lang,
            "definition": "",
            "added_by": "student",
            "added_ts": time.strftime("%Y-%m-%d", time.gmtime()),
            "suggested_by_hash": self.db.hash_user(user_id),
            "suggested_ts": int(time.time()),
            "status": "pending",
        }
        _write_json(path, data)

    def _handle_privacy_command(self, job: _Job, body: str) -> bool:
        """Returns True if the message was handled as a !privacy command."""
        from student_bot.lang import detect

        parts = body.lower().split()
        if not parts or parts[0] != "!privacy":
            return False
        lang = detect(body) if body else "sv"
        sub = parts[1] if len(parts) > 1 else "status"
        if sub == "off":
            self.db.set_opt_out(job.user_id, True)
            self._post(
                job.channel_id, PRIVACY_OFF_EN if lang == "en" else PRIVACY_OFF_SV, job.root_id
            )
        elif sub == "on":
            self.db.set_opt_out(job.user_id, False)
            self._post(
                job.channel_id, PRIVACY_ON_EN if lang == "en" else PRIVACY_ON_SV, job.root_id
            )
        else:
            opted = self.db.is_opted_out(job.user_id)
            if lang == "en":
                state = "off (not stored)" if opted else "on (stored anonymously)"
                msg = PRIVACY_STATUS_EN.format(state=state)
            else:
                state = "av (lagras inte)" if opted else "på (lagras anonymt)"
                msg = PRIVACY_STATUS_SV.format(state=state)
            self._post(job.channel_id, msg, job.root_id)
        return True

    def _handle_job(self, job: _Job):
        # First-DM GDPR notice (only in DMs, only once per user).
        if job.channel_type == "D" and not self.db.has_disclosed(job.user_id):
            from student_bot.lang import detect

            lang = detect(job.question)
            notice = GDPR_NOTICE_EN if lang == "en" else GDPR_NOTICE_SV
            self._post(job.channel_id, notice, job.root_id)
            self.db.mark_disclosed(job.user_id)

        # !privacy and !jargon commands short-circuit the RAG pipeline.
        if self._handle_privacy_command(job, job.question.strip()):
            return
        if self._handle_jargon_command(job, job.question.strip()):
            return

        # "Thinking" indicator: react to the user's post while the LLM runs.
        # Removed in `finally` so a crash in the pipeline still clears it.
        # Our own on_event filter (`reaction.user_id == bot_user_id`) keeps
        # this reaction from being recorded as feedback.
        self._react(job.user_post_id, THINKING_EMOJI)
        try:
            result = answer(job.question, cfg=self.cfg, rate_limit_key=job.user_id)
            bot_post_id = self._post(job.channel_id, result.rendered, job.root_id)
        finally:
            self._unreact(job.user_post_id, THINKING_EMOJI)

        chunk_ids = [c.chunk_id for c in result.retrieval.reranked]
        qa_id = self.db.record_qa(
            user_id=job.user_id,
            channel_type=job.channel_type,
            channel_id=job.channel_id,
            bot_post_id=bot_post_id,
            root_id=job.root_id,
            question=job.question,
            lang=result.lang,
            retrieved_chunk_ids=chunk_ids,
            rerank_top1=result.gate.top1,
            rerank_meanK=result.gate.meanK,
            distinct_sources=result.gate.distinct_sources,
            gate_pass=result.gate.passed,
            gate_reason=result.gate.reason,
            answer=result.answer,
            latency_ms=result.latency_ms,
            question_expanded=result.expanded_question or None,
            jargon_hits=[e.key for e in result.jargon_hits] or None,
        )

        # Topic classification runs AFTER the user has their answer so it
        # never adds visible latency. Skip for opted-out users (qa_id is None).
        if qa_id is not None and self.cfg.topics.enabled:
            try:
                from student_bot.bot.topics import classify

                topic, confidence = classify(self.cfg, job.question, result.lang)
                self.db.update_topic(qa_id, topic, confidence)
            except Exception:
                log.exception("topic classification failed")

    # --- websocket event handling ---

    def _should_handle_post(self, post: dict, channel_type: str) -> bool:
        # Filter out our own posts and bot posts.
        if post.get("user_id") == self.bot_user_id:
            return False
        props = post.get("props") or {}
        if props.get("from_bot") in ("true", True):
            return False
        # System messages.
        if (post.get("type") or "").startswith("system_"):
            return False

        if channel_type == "D":
            return True

        # Channel: must mention us.
        msg = post.get("message", "")
        return self.cfg.mattermost.trigger_mention in msg

    def _strip_mention(self, message: str) -> str:
        m = self.cfg.mattermost.trigger_mention
        return message.replace(m, "").strip() if m in message else message.strip()

    def _root_id_for_reply(self, post: dict) -> str:
        return post.get("root_id") or post.get("id") or ""

    async def on_event(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except Exception:
            return

        etype = event.get("event")
        data = event.get("data") or {}

        if etype == "posted":
            channel_type = data.get("channel_type", "")
            try:
                post = json.loads(data.get("post", "{}"))
            except Exception:
                return
            if not self._should_handle_post(post, channel_type):
                return
            question = self._strip_mention(post.get("message", ""))
            if not question:
                return
            self.queue.put(
                _Job(
                    user_id=post.get("user_id", ""),
                    channel_id=post.get("channel_id", ""),
                    channel_type=channel_type or "O",
                    root_id=self._root_id_for_reply(post),
                    user_post_id=post.get("id", ""),
                    question=question,
                )
            )

        elif etype == "reaction_added":
            try:
                reaction = json.loads(data.get("reaction", "{}"))
            except Exception:
                return
            if reaction.get("user_id") == self.bot_user_id:
                return
            emoji = reaction.get("emoji_name", "")
            sentiment = (
                "positive"
                if emoji in POSITIVE_EMOJI
                else "negative"
                if emoji in NEGATIVE_EMOJI
                else None
            )
            if not sentiment:
                return
            bot_post_id = reaction.get("post_id", "")
            if not bot_post_id:
                return
            if self.db.lookup_qa_by_bot_post(bot_post_id) is None:
                return  # reaction on a non-bot post; ignore
            self.db.record_feedback(
                bot_post_id=bot_post_id,
                user_id=reaction.get("user_id", ""),
                sentiment=sentiment,
                emoji=emoji,
            )

    # --- websocket reconnect loop ---

    def serve(self):
        worker = threading.Thread(target=self.worker_loop, daemon=True, name="bot-worker")
        worker.start()

        backoff = 1
        cap = max(1, self.cfg.mattermost.reconnect_max_seconds)
        while not self.shutdown.is_set():
            try:
                self.login()
                backoff = 1
                log.info("connecting websocket…")
                assert self.driver is not None
                self.driver.init_websocket(self.on_event)
            except Exception:
                log.exception("websocket loop crashed; will reconnect")
            if self.shutdown.is_set():
                break
            log.info("sleeping %ds before reconnect", backoff)
            self.shutdown.wait(backoff)
            backoff = min(cap, backoff * 2)

        log.info("shutting down; draining queue")
        self.queue.join()


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)],
    )


def main():
    _setup_logging()
    cfg = get_config()
    bot = StudentBot(cfg)

    def handle_sig(signum, frame):
        log.info("signal %s; shutting down", signum)
        bot.shutdown.set()
        # Try to nudge the websocket.
        if bot.driver is not None:
            try:
                bot.driver.disconnect()
            except Exception:
                pass

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    bot.serve()
    sys.exit(0)


if __name__ == "__main__":
    main()


__all__ = ["StudentBot", "main"]
