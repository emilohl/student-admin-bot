"""FastAPI web app: chat UI, corpus file server, stats page.

By default binds to 127.0.0.1 (localhost-only). To expose externally:
    WEB_BIND_HOST=0.0.0.0 WEB_AUTH_ENABLED=true \\
    WEB_ACCESS_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \\
    student-bot-web

When auth is enabled, two factors are required:
1. The visit URL must include `?access=<WEB_ACCESS_TOKEN>` (capability URL).
2. Each request must carry HTTP Basic Auth credentials from `data/web_users`.
   Add a user with `student-bot-mkuser <username>`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
from pathlib import Path

import click
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rich.logging import RichHandler
from starlette.middleware.sessions import SessionMiddleware

from student_bot.bot.citations import confidence_badge
from student_bot.bot.memory import ConversationMemory
from student_bot.bot.pipeline import answer
from student_bot.bot.topics import classify
from student_bot.config import Config, get_config
from student_bot.jargon import Jargon, _nfc_lower, _read_json, _write_json
from student_bot.logging_db import LogDB
from student_bot.web.auth import require_access


log = logging.getLogger("student_bot.web")

WEB_PKG_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_PKG_DIR / "static"


# --- request schemas ---


class SessionRequest(BaseModel):
    name: str = ""
    session_id: str = ""
    opt_out: bool = False


class ChatRequest(BaseModel):
    question: str
    name: str = "Anonym"
    session_id: str = "default"
    opt_out: bool = False


class FeedbackRequest(BaseModel):
    qa_id: int
    sentiment: str  # "positive" | "negative"


class ResetRequest(BaseModel):
    session_id: str


class JargonSuggestRequest(BaseModel):
    term: str
    expansion: str
    definition: str = ""
    lang: str = "sv"


# --- app factory ---


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or get_config()
    app = FastAPI(title="student-bot")
    memory = ConversationMemory(cfg)
    db = LogDB(cfg)

    session_secret = os.environ.get("WEB_SESSION_SECRET") or secrets.token_hex(16)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        same_site="lax",
        https_only=False,
        max_age=cfg.web.session_idle_minutes * 60,
    )

    docs_dir = cfg.absolute(cfg.paths.docs_dir).resolve()
    if docs_dir.exists() and cfg.web.doc_base_url:
        app.mount(
            cfg.web.doc_base_url,
            StaticFiles(directory=str(docs_dir), follow_symlink=True),
            name="docs",
        )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # --- pages ---

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        require_access(request, cfg)
        if name := request.query_params.get("name"):
            request.session["name"] = name
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request):
        require_access(request, cfg)
        return _about_page(cfg)

    @app.get("/stats", response_class=HTMLResponse)
    def stats(request: Request):
        require_access(request, cfg)
        return _stats_page(cfg, db)

    @app.get("/glossary", response_class=HTMLResponse)
    def glossary(request: Request):
        require_access(request, cfg)
        return _glossary_page(cfg)

    @app.post("/api/jargon/suggest")
    def jargon_suggest(request: Request, payload: JargonSuggestRequest):
        ctx = require_access(request, cfg)
        term = payload.term.strip()
        expansion = payload.expansion.strip()
        if not term or not expansion:
            raise HTTPException(400, "term and expansion are required")
        if len(term) > 64 or len(expansion) > 200 or len(payload.definition) > 500:
            raise HTTPException(400, "field too long")
        path = cfg.absolute(Path(cfg.jargon.proposals_file))
        data = _read_json(path) if path.exists() else {"version": 1, "entries": {}}
        entries = data.setdefault("entries", {})
        entries[_nfc_lower(term)] = {
            "term": term,
            "expansion": expansion,
            "lang": payload.lang or "sv",
            "definition": payload.definition.strip(),
            "added_by": "web",
            "added_ts": __import__("time").strftime("%Y-%m-%d"),
            "suggested_by_hash": db.hash_user(ctx.user or "anonymous-web"),
            "suggested_ts": int(__import__("time").time()),
            "status": "pending",
        }
        _write_json(path, data)
        return {"ok": True}

    # --- API ---

    @app.get("/api/health")
    def health():
        return {"status": "ok", "auth_enabled": cfg.web.auth_enabled}

    @app.post("/api/session")
    def session_set(request: Request, payload: SessionRequest):
        require_access(request, cfg)
        if payload.name:
            request.session["name"] = payload.name
        request.session["opt_out"] = payload.opt_out
        web_user_id = _web_user_id(payload, request.session.get("user"))
        db.set_opt_out(web_user_id, payload.opt_out)
        if not db.has_disclosed(web_user_id):
            db.mark_disclosed(web_user_id)
        return {"ok": True}

    @app.post("/api/reset")
    def reset(request: Request, payload: ResetRequest):
        require_access(request, cfg)
        web_user_id = _web_user_id_from_request(request, payload.session_id)
        memory.clear(web_user_id, "default")
        return {"ok": True}

    @app.post("/api/chat")
    def chat(request: Request, payload: ChatRequest):
        ctx = require_access(request, cfg)
        web_user_id = _web_user_id(payload, ctx.user)
        if payload.opt_out:
            db.set_opt_out(web_user_id, True)
        elif db.is_opted_out(web_user_id):
            db.set_opt_out(web_user_id, False)

        return StreamingResponse(
            _stream_answer(cfg, db, memory, payload, web_user_id),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.post("/api/feedback")
    def feedback(request: Request, payload: FeedbackRequest):
        ctx = require_access(request, cfg)
        if payload.sentiment not in ("positive", "negative"):
            raise HTTPException(400, "bad sentiment")
        # Use a synthetic post id derived from qa_id so the feedback table
        # links correctly.
        bot_post_id = f"web:{payload.qa_id}"
        # Ensure the qa row exists and gets its bot_post_id stamped.
        with db._connect() as conn:  # noqa: SLF001 (intentional lightweight write)
            row = conn.execute(
                "SELECT bot_post_id FROM qa_log WHERE id = ?", (payload.qa_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "qa not found")
            if not row[0]:
                conn.execute(
                    "UPDATE qa_log SET bot_post_id = ? WHERE id = ?", (bot_post_id, payload.qa_id)
                )
                conn.commit()
            else:
                bot_post_id = row[0]
        db.record_feedback(
            bot_post_id=bot_post_id,
            user_id=ctx.user or "web",
            sentiment=payload.sentiment,
            emoji=("+1" if payload.sentiment == "positive" else "-1"),
        )
        return {"ok": True}

    return app


def _web_user_id(payload, http_user: str | None) -> str:
    """Stable identifier for a web visitor: prefer Basic Auth username, else
    fall back to (name|session_id) so anonymous visitors still get a stable key
    for memory and opt-out."""
    if http_user and http_user != "anonymous":
        return f"basic:{http_user}"
    sid = payload.session_id or "default"
    name = payload.name or "Anonym"
    return f"web:{name}:{sid}"


def _web_user_id_from_request(request: Request, session_id: str) -> str:
    user = request.session.get("user") or "anonymous"
    name = request.session.get("name") or "Anonym"
    if user != "anonymous":
        return f"basic:{user}"
    return f"web:{name}:{session_id or 'default'}"


def _stream_answer(
    cfg: Config, db: LogDB, memory: ConversationMemory, payload: ChatRequest, web_user_id: str
):
    """Generator producing SSE blocks. Streams answer tokens and a final
    `meta` event with confidence, qa_id, and gate info."""
    history = memory.get(web_user_id, "default")

    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def on_token(delta: str):
        queue.put_nowait(("token", delta))

    def on_thinking(starting: bool):
        queue.put_nowait(("thinking", "start" if starting else "end"))

    def run_in_thread():
        try:
            result = answer(
                payload.question,
                history=history,
                cfg=cfg,
                on_token=on_token,
                on_thinking=on_thinking,
                rate_limit_key=web_user_id,
            )
        except Exception as e:
            queue.put_nowait(("token", f"\n[error: {e}]"))
            queue.put_nowait((sentinel, None))
            return
        queue.put_nowait((sentinel, result))

    threading.Thread(target=run_in_thread, daemon=True).start()

    async def gen():
        result = None
        while True:
            kind, value = await queue.get()
            if kind is sentinel:
                result = value
                break
            if kind == "thinking":
                yield _sse("thinking", value)
            else:
                yield _sse("token", value)

        if result is None:
            return

        # Persist to memory and DB after the stream finishes.
        if (
            result.answered
            or result.meta_fallback
            or result.gate.reason == "programme_clarification"
        ):
            memory.append(web_user_id, "default", "user", payload.question)
            memory.append(web_user_id, "default", "assistant", result.answer)

        chunk_ids = [c.chunk_id for c in result.retrieval.reranked]
        qa_id = db.record_qa(
            user_id=web_user_id,
            channel_type="W",  # 'W' = web
            channel_id=payload.session_id,
            bot_post_id=None,
            root_id=None,
            question=payload.question,
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

        if qa_id is not None and cfg.topics.enabled:
            try:
                topic, conf = classify(cfg, payload.question, result.lang)
                db.update_topic(qa_id, topic, conf)
            except Exception:
                log.exception("topic classify failed")

        meta = {
            "qa_id": qa_id,
            "lang": result.lang,
            "gate": result.gate.reason,
            "answered": result.answered,
            "confidence": confidence_badge(result.lang, result.gate.top1),
            "confidence_level": _conf_class(result.gate.top1),
            "latency_ms": result.latency_ms,
            "source_urls": result.source_urls,
            "stale_cache_days": result.stale_cache_days,
        }
        yield _sse("meta", json.dumps(meta))

    return gen()


def _sse(event: str, data: str) -> bytes:
    out = []
    out.append(f"event: {event}")
    for line in data.split("\n"):
        out.append(f"data: {line}")
    out.append("")
    out.append("")
    return ("\n".join(out)).encode("utf-8")


def _conf_class(top1: float) -> str:
    if top1 >= 3.0:
        return "high"
    if top1 >= 0.5:
        return "medium"
    return "low"


# --- about + stats pages (server-rendered) ---


# Shared experimental-service notice. Same markup the chat page uses, so
# notice.js dismissal state is shared across all pages on this origin.
# Body text is filled in by i18n.js based on the user's language choice.
_NOTICE_HTML = """\
<div class="notice" role="note">
  <div class="notice-body">
    <p><strong data-i18n="notice.title"></strong><span data-i18n="notice.body"></span></p>
  </div>
  <button class="notice-close" type="button" data-i18n-aria="notice.close.aria">×</button>
</div>
"""

# Shared header: centered brand cluster (KTH logo + title + Fraktur F)
# with the language switch on the right. {tagline_html} is replaced per page.
_HEADER_HTML = """\
<header>
  <div class="brand">
    <img src="/static/KTH_logo_RGB_bla.svg" alt="KTH" class="logo logo-kth">
    <div class="brand-text">
      <h1 data-i18n="brand.name"></h1>{tagline_html}
    </div>
    <img src="/static/FrakturF2020.svg" alt="Fysiksektionen" class="logo logo-fyssek">
  </div>
  <div class="lang-switch" role="group" aria-label="Language">
    <button type="button" data-lang="sv">SV</button>
    <button type="button" data-lang="en">EN</button>
  </div>
</header>
"""

# Loaded into <head> on every server-rendered page, before notice.js, so
# data-i18n attributes are translated before any other scripts run.
_NOTICE_SCRIPT = (
    '<script src="/static/i18n.js?v=14"></script>'
    '<script src="/static/notice.js?v=14" defer></script>'
)


def _about_page(cfg: Config) -> HTMLResponse:
    link = cfg.fallback.counselor_link
    # Counselor link is config-driven, not language-bound, so we render it
    # server-side and append it after the translatable tip text.
    cl_html = f' (<a href="{link}">{link}</a>)' if link else ""
    body = f"""
<!doctype html><html lang="sv"><head><meta charset="utf-8"><title>student-bot</title>
<link rel="stylesheet" href="/static/style.css?v=14">{_NOTICE_SCRIPT}</head>
<body>{_HEADER_HTML.format(tagline_html="")}<main>{_NOTICE_HTML}<div class="card">
<h2 data-i18n="about.h2.what"></h2>
<p data-i18n="about.what.body"></p>

<h2 data-i18n="about.h2.tips"></h2>
<ol>
<li data-i18n="about.tip1"></li>
<li data-i18n="about.tip2"></li>
<li><span data-i18n="about.tip3"></span>{cl_html}</li>
<li data-i18n="about.tip4"></li>
<li data-i18n="about.tip5"></li>
</ol>
<p><a href="/" data-i18n="about.back"></a></p>
</div></main></body></html>
"""
    return HTMLResponse(body)


def _glossary_page(cfg: Config) -> HTMLResponse:
    j = Jargon.from_config(cfg)
    rows = (
        "".join(
            f"<tr><td><code>{_h(e.term)}</code></td><td>{_h(e.expansion)}</td>"
            f"<td>{_h(e.definition) or '—'}</td><td>{_h(e.lang)}</td></tr>"
            for e in j.all_entries()
        )
        or '<tr><td colspan="4" data-i18n="glossary.empty"></td></tr>'
    )
    body = f"""
<!doctype html><html lang="sv"><head><meta charset="utf-8"><title>student-bot</title>
<link rel="stylesheet" href="/static/style.css?v=14">{_NOTICE_SCRIPT}</head>
<body>{_HEADER_HTML.format(tagline_html='<p class="tagline" data-i18n="glossary.tagline"></p>')}
<main>{_NOTICE_HTML}<div class="card">
<table border="1" cellpadding="6" cellspacing="0" style="width:100%; border-collapse: collapse;">
<thead><tr><th data-i18n="glossary.th.term"></th><th data-i18n="glossary.th.meaning"></th><th data-i18n="glossary.th.def"></th><th data-i18n="glossary.th.lang"></th></tr></thead>
<tbody>{rows}</tbody></table>

<h2 style="margin-top: 24px" data-i18n="glossary.suggest.h2"></h2>
<form id="jargon-form" onsubmit="return submitJargon(event);">
  <label><span data-i18n="glossary.suggest.term"></span> <input name="term" required maxlength="64" placeholder="t.ex. KS"></label>
  <label><span data-i18n="glossary.suggest.expansion"></span> <input name="expansion" required maxlength="200" placeholder="kontrollskrivning"></label>
  <label><span data-i18n="glossary.suggest.definition"></span> <input name="definition" maxlength="500"></label>
  <label><span data-i18n="glossary.suggest.lang"></span>
    <select name="lang"><option value="sv">sv</option><option value="en">en</option><option value="any">any</option></select>
  </label>
  <button type="submit" data-i18n="glossary.suggest.submit"></button>
  <span id="jargon-status" class="status"></span>
</form>
<p style="margin-top: 16px"><a href="/" data-i18n="glossary.back"></a></p>
</div></main>
<script>
async function submitJargon(e) {{
  e.preventDefault();
  const f = e.target;
  const status = document.getElementById('jargon-status');
  status.textContent = window.t ? window.t('glossary.status.sending') : 'skickar…';
  const r = await fetch('/api/jargon/suggest', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      term: f.term.value,
      expansion: f.expansion.value,
      definition: f.definition.value,
      lang: f.lang.value,
    }}),
  }});
  if (r.ok) {{ status.textContent = window.t ? window.t('glossary.status.ok') : 'tack — förslaget köades för granskning'; f.reset(); }}
  else {{ status.textContent = (window.t ? window.t('glossary.status.error') : 'fel') + ': ' + r.status; }}
  return false;
}}
</script>
</body></html>
"""
    return HTMLResponse(body)


def _h(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _stats_page(cfg: Config, db: LogDB) -> HTMLResponse:
    overall = db.overall_counts()
    by_topic = db.stats_by_topic()
    rows = (
        "".join(
            f"<tr><td>{r['topic']}</td><td>{r['n']}</td><td>{r['answered']}</td>"
            f"<td>{r['avg_latency_ms']}</td>"
            f"<td>{r['thumbs_up']}</td><td>{r['thumbs_down']}</td></tr>"
            for r in by_topic
        )
        or '<tr><td colspan="6" data-i18n="stats.empty"></td></tr>'
    )
    body = f"""
<!doctype html><html lang="sv"><head><meta charset="utf-8"><title>student-bot</title>
<link rel="stylesheet" href="/static/style.css?v=14">{_NOTICE_SCRIPT}</head>
<body>{_HEADER_HTML.format(tagline_html="")}<main>{_NOTICE_HTML}<div class="card">
<p data-i18n="stats.summary"
   data-logged="{overall["logged"]}"
   data-answered="{overall["answered"]}"
   data-latency="{overall["avg_latency_ms"]}"
   data-anon="{overall["anon"]}"></p>
<table border="1" cellpadding="6" cellspacing="0">
<thead><tr><th data-i18n="stats.th.topic"></th><th data-i18n="stats.th.n"></th><th data-i18n="stats.th.answered"></th>
<th data-i18n="stats.th.avgms"></th><th>👍</th><th>👎</th></tr></thead>
<tbody>{rows}</tbody></table>
<p><a href="/" data-i18n="stats.back"></a></p>
</div></main></body></html>
"""
    return HTMLResponse(body)


# --- CLI ---


@click.command()
@click.option("--host", default=None, help="Override bind host (default from config).")
@click.option("--port", type=int, default=None, help="Override port.")
@click.option("--reload", is_flag=True, help="Enable autoreload (dev).")
def main(host: str | None, port: int | None, reload: bool):
    logging.basicConfig(
        level=logging.INFO, format="%(message)s", datefmt="[%X]", handlers=[RichHandler()]
    )
    for noisy in ("httpx", "httpcore", "huggingface_hub", "sentence_transformers", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = get_config()
    bind_host = host or cfg.web.bind_host
    bind_port = port or cfg.web.port

    if cfg.web.auth_enabled and not os.environ.get("WEB_ACCESS_TOKEN"):
        log.error("WEB_AUTH_ENABLED=true but WEB_ACCESS_TOKEN is not set; aborting.")
        raise SystemExit(2)

    if cfg.web.auth_enabled:
        users_path = cfg.absolute(Path(cfg.web.users_file))
        if not users_path.exists():
            log.error(
                "auth enabled but users file %s does not exist; "
                "create one with `student-bot-mkuser <name>`.",
                users_path,
            )
            raise SystemExit(2)
        token = os.environ.get("WEB_ACCESS_TOKEN", "")
        log.info("auth enabled. login URL: http://%s:%s/?access=%s", bind_host, bind_port, token)
    else:
        log.info("auth disabled. binding to http://%s:%s", bind_host, bind_port)
        if bind_host != "127.0.0.1":
            log.warning(
                "WEB_BIND_HOST is %s but auth is disabled — anyone reaching "
                "this host can chat. Set WEB_AUTH_ENABLED=true.",
                bind_host,
            )

    uvicorn.run(
        "student_bot.web.app:create_app",
        host=bind_host,
        port=bind_port,
        factory=True,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()


__all__ = ["create_app", "main"]
