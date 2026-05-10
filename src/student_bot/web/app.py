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
import subprocess
import threading
import time
from pathlib import Path

import click
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rich.logging import RichHandler
from starlette.middleware.sessions import SessionMiddleware

from student_bot.bot.citations import (
    build_doc_url,
    confidence_badge,
    format_source_display_label,
)
from student_bot.bot.memory import ConversationMemory
from student_bot.bot.pipeline import answer
from student_bot.bot.topics import classify
from student_bot.config import Config, get_config
from student_bot.jargon import Jargon, _nfc_lower, _read_json, _write_json
from student_bot.logging_db import LogDB
from student_bot.version import get_version
from student_bot.web.auth import list_usernames, require_access
from student_bot.web.md_render import render_file


log = logging.getLogger("student_bot.web")

WEB_PKG_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_PKG_DIR / "static"
HOST_METRICS_FILE = Path("data/host_metrics.json")
HOST_METRICS_START_CMD = "uv run student-bot-host-metrics"
HOST_METRICS_STOP_CMD = "pkill -f student-bot-host-metrics"


def _perf_panel_enabled(cfg: Config) -> bool:
    # Backward-compatible guard: older config schema instances may not carry
    # this attribute yet in mixed-image/dev-mount setups.
    return bool(getattr(cfg.web, "performance_panel_enabled", False))


def _normalize_base_path(base_path: str) -> str:
    bp = (base_path or "").strip()
    if not bp:
        return ""
    if not bp.startswith("/"):
        bp = "/" + bp
    return bp.rstrip("/")


def _join_base(base_path: str, path: str) -> str:
    if not path:
        return base_path or ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    p = path if path.startswith("/") else "/" + path
    if not base_path:
        return p
    return f"{base_path}{p}"


def _source_http_url(
    rel_source: str,
    page_start: int | None,
    doc_base_url: str,
    *,
    source_url: str = "",
    md_render_base_url: str = "",
) -> str:
    """URL for the web UI / SSE `sources` list. Never drop obvious http(s) links.

    Curated local markdown files are routed through `md_render_base_url` so
    they open as styled HTML pages with attribution, rather than raw text via
    the static mount. Web-imported markdown (under `web_import/`) keeps its
    upstream `source_url` preference.
    """
    rs = (rel_source or "").strip()
    if (
        md_render_base_url
        and rs
        and rs.lower().endswith(".md")
        and not rs.startswith("web_import/")
        and not rs.startswith("https://")
        and not rs.startswith("http://")
        and not (source_url or "").startswith(("http://", "https://"))
    ):
        from urllib.parse import quote as _quote

        return f"{md_render_base_url.rstrip('/')}/{_quote(rs, safe='/')}"
    u = build_doc_url(rel_source, page_start, doc_base_url, source_url=source_url)
    if u:
        return u
    for raw in (source_url or "", rel_source or ""):
        s = (raw or "").strip()
        if s.startswith("https://") or s.startswith("http://"):
            return s
    return ""


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
    base_path = _normalize_base_path(getattr(cfg.web, "base_path", ""))

    if cfg.web.doc_base_url:
        cfg.web.doc_base_url = _join_base(base_path, cfg.web.doc_base_url)
    if cfg.web.md_render_base_url:
        cfg.web.md_render_base_url = _join_base(base_path, cfg.web.md_render_base_url)

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
    app.mount(
        _join_base(base_path, "/static"), StaticFiles(directory=str(STATIC_DIR)), name="static"
    )

    # --- pages ---

    @app.get(_join_base(base_path, "/"), response_class=HTMLResponse)
    def index(request: Request):
        require_access(request, cfg)
        if name := request.query_params.get("name"):
            request.session["name"] = name
        return FileResponse(STATIC_DIR / "index.html")

    @app.get(_join_base(base_path, "/about"), response_class=HTMLResponse)
    def about(request: Request):
        require_access(request, cfg)
        return _about_page(cfg, base_path)

    @app.get(_join_base(base_path, "/stats"), response_class=HTMLResponse)
    def stats(request: Request, ch: str = "all"):
        require_access(request, cfg)
        return _stats_page(cfg, db, base_path, channel=_normalize_channel(ch))

    @app.get(_join_base(base_path, "/stats/series"))
    def stats_series(request: Request, range: str = "72h", ch: str = "all"):
        require_access(request, cfg)
        return _stats_series_response(db, range, channel=_normalize_channel(ch))

    @app.get(_join_base(base_path, "/glossary"), response_class=HTMLResponse)
    def glossary(request: Request):
        require_access(request, cfg)
        return _glossary_page(cfg, base_path)

    if cfg.web.md_render_base_url:

        @app.get(
            cfg.web.md_render_base_url.rstrip("/") + "/{rel_source:path}",
            response_class=HTMLResponse,
        )
        def render_doc(request: Request, rel_source: str):
            require_access(request, cfg)
            return _md_doc_page(cfg, docs_dir, rel_source, base_path)

    @app.post(_join_base(base_path, "/api/jargon/suggest"))
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

    @app.get(_join_base(base_path, "/api/health"))
    def health():
        return {
            "status": "ok",
            "auth_enabled": cfg.web.auth_enabled,
            "performance_panel_enabled": _perf_panel_enabled(cfg),
        }

    @app.get(_join_base(base_path, "/api/system-load"))
    def system_load(request: Request):
        require_access(request, cfg)
        if not _perf_panel_enabled(cfg):
            return {"performance_panel_enabled": False}
        return {
            "performance_panel_enabled": True,
            "system_load": _system_load_snapshot(),
            "host_system_load": _host_load_snapshot(cfg),
        }

    @app.on_event("startup")
    def startup_notice():
        if not _perf_panel_enabled(cfg):
            return
        log.info("performance panel enabled; start host metrics collector on host.")
        log.info("command: %s", HOST_METRICS_START_CMD)

    @app.on_event("shutdown")
    def shutdown_notice():
        if not _perf_panel_enabled(cfg):
            return
        log.info("web app stopping; stop host metrics collector if still running.")
        log.info("command: %s", HOST_METRICS_STOP_CMD)

    @app.post(_join_base(base_path, "/api/session"))
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

    @app.post(_join_base(base_path, "/api/reset"))
    def reset(request: Request, payload: ResetRequest):
        require_access(request, cfg)
        web_user_id = _web_user_id_from_request(request, payload.session_id)
        memory.clear(web_user_id, "default")
        return {"ok": True}

    @app.post(_join_base(base_path, "/api/chat"))
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

    @app.post(_join_base(base_path, "/api/feedback"))
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
    program_prior = memory.get_program_code(web_user_id, "default")

    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def on_token(delta: str):
        queue.put_nowait(("token", delta))

    def on_jargon_prefix(delta: str):
        queue.put_nowait(("jargon", delta))

    def on_thinking(starting: bool):
        queue.put_nowait(("thinking", "start" if starting else "end"))

    def run_in_thread():
        try:
            result = answer(
                payload.question,
                history=history,
                cfg=cfg,
                on_token=on_token,
                on_jargon_prefix=on_jargon_prefix,
                on_thinking=on_thinking,
                rate_limit_key=web_user_id,
                program_prior=program_prior,
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
            elif kind == "jargon":
                yield _sse("jargon", value)
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
        if result.program_code:
            memory.set_program_code(web_user_id, "default", result.program_code)

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
            prompt_tokens=result.context_tokens_est,
            gen_tokens=result.gen_tokens_est,
        )

        if qa_id is not None and cfg.topics.enabled:
            try:
                topic, conf = classify(cfg, payload.question, result.lang)
                db.update_topic(qa_id, topic, conf)
            except Exception:
                log.exception("topic classify failed")

        # Structured source metadata for the web client. This avoids relying
        # on markdown tail parsing for citation mapping/rendering.
        sources: list[dict] = []
        seen: set[tuple] = set()
        for c in result.cited_chunks:
            key = (c.doc_title, c.section_path or None, c.page_start)
            if key in seen:
                continue
            seen.add(key)
            label = format_source_display_label(cfg, c)
            url = _source_http_url(
                c.rel_source,
                c.page_start,
                cfg.web.doc_base_url,
                source_url=c.source_url,
                md_render_base_url=cfg.web.md_render_base_url,
            )
            sources.append({"n": len(sources) + 1, "label": label, "url": url})

        # Include broader retrieval candidates so the web client can still
        # resolve inline raw citations like "[docslug · section]" when they
        # coexist with server-numbered "[N]" markers in the same answer.
        source_candidates: list[dict] = []
        seen_candidates: set[tuple] = set()
        for c in result.retrieval.reranked:
            key = (c.doc_title, c.section_path or None, c.page_start)
            if key in seen_candidates:
                continue
            seen_candidates.add(key)
            label = format_source_display_label(cfg, c)
            url = _source_http_url(
                c.rel_source,
                c.page_start,
                cfg.web.doc_base_url,
                source_url=c.source_url,
                md_render_base_url=cfg.web.md_render_base_url,
            )
            source_candidates.append(
                {
                    # Non-numeric id on purpose: keeps these out of the
                    # authoritative [N] map (which comes from `sources` above)
                    # while still allowing lookup by label text.
                    "n": f"cand:{len(source_candidates) + 1}",
                    "label": label,
                    "url": url,
                }
            )

        meta = {
            "qa_id": qa_id,
            "lang": result.lang,
            "gate": result.gate.reason,
            "answered": result.answered,
            "numbered_body": result.numbered_body,
            "confidence": confidence_badge(result.lang, result.gate.top1),
            "confidence_level": _conf_class(result.gate.top1),
            "latency_ms": result.latency_ms,
            "source_urls": result.source_urls,
            "sources": sources,
            "source_candidates": source_candidates,
            "stale_cache_days": result.stale_cache_days,
            "performance_panel_enabled": _perf_panel_enabled(cfg),
        }
        if _perf_panel_enabled(cfg):
            meta.update(
                {
                    "context_tokens_est": result.context_tokens_est,
                    "context_tokens_limit": result.context_tokens_limit,
                    "gen_tokens_est": result.gen_tokens_est,
                    "ttft_ms": result.ttft_ms,
                    "gen_tps": result.gen_tps,
                    "system_load": _system_load_snapshot(),
                    "host_system_load": _host_load_snapshot(cfg),
                }
            )
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


def _gpu_load_snapshot() -> dict[str, float] | None:
    # Optional: best-effort NVIDIA telemetry when available.
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            text=True,
        ).strip()
        if not out:
            return None
        first = out.splitlines()[0]
        util_s, mem_used_s, mem_total_s = [p.strip() for p in first.split(",")[:3]]
        mem_used = float(mem_used_s)
        mem_total = float(mem_total_s)
        mem_pct = (mem_used / mem_total * 100.0) if mem_total > 0 else 0.0
        return {"util_pct": float(util_s), "mem_pct": mem_pct}
    except Exception:
        return None


def _system_load_snapshot() -> dict:
    now = int(time.time() * 1000)
    cpu_pct: float | None = None
    mem_pct: float | None = None

    try:
        load1 = os.getloadavg()[0]
        cpu_count = max(1, os.cpu_count() or 1)
        cpu_pct = max(0.0, min(100.0, (load1 / cpu_count) * 100.0))
    except Exception:
        cpu_pct = None

    # Linux container path.
    try:
        meminfo = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition(":")
                meminfo[k.strip()] = v.strip()
        total = float(meminfo.get("MemTotal", "0 kB").split()[0] or 0.0)
        avail = float(meminfo.get("MemAvailable", "0 kB").split()[0] or 0.0)
        if total > 0:
            mem_pct = max(0.0, min(100.0, ((total - avail) / total) * 100.0))
    except Exception:
        mem_pct = None

    return {
        "ts_ms": now,
        "cpu_pct": cpu_pct,
        "mem_pct": mem_pct,
        "gpu": _gpu_load_snapshot(),
    }


def _host_load_snapshot(cfg: Config) -> dict | None:
    path = cfg.absolute(HOST_METRICS_FILE)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        ts = int(data.get("ts_ms", 0))
        # Ignore stale host samples (>10s old).
        if ts and int(time.time() * 1000) - ts > 10_000:
            return None
        return {
            "ts_ms": ts,
            "cpu_pct": data.get("cpu_pct"),
            "mem_pct": data.get("mem_pct"),
            "gpu": data.get("gpu"),
        }
    except Exception:
        return None


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
    <img src="{static_prefix}/KTH_logo_RGB_bla.svg" alt="KTH" class="logo logo-kth">
    <div class="brand-text">
      <h1 data-i18n="brand.name"></h1>{tagline_html}
    </div>
    <img src="{static_prefix}/FrakturF2020.svg" alt="Fysiksektionen" class="logo logo-fyssek">
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
    '<script src="{static_prefix}/i18n.js?v=26"></script>'
    '<script src="{static_prefix}/notice.js?v=26" defer></script>'
)


def _about_page(cfg: Config, base_path: str = "") -> HTMLResponse:
    link = cfg.fallback.counselor_link
    static_prefix = _join_base(base_path, "/static")
    home = _join_base(base_path, "/") or "/"
    # Counselor link is config-driven, not language-bound, so we render it
    # server-side and append it after the translatable tip text.
    cl_html = f' (<a href="{link}">{link}</a>)' if link else ""
    version = get_version()
    version_html = (
        f' (<a href="{version.link}" target="_blank" rel="noopener">{version.display}</a>)'
        if version.display
        else ""
    )
    body = f"""
<!doctype html><html lang="sv"><head><meta charset="utf-8"><title>student-bot</title>
<link rel="stylesheet" href="{static_prefix}/style.css?v=26">{_NOTICE_SCRIPT.format(static_prefix=static_prefix)}</head>
<body>{_HEADER_HTML.format(tagline_html="", static_prefix=static_prefix)}<main>{_NOTICE_HTML}<div class="card">
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
<div class="github-link">
  <a href="https://github.com/cohm/student-admin-bot" target="_blank" rel="noopener">
    <svg class="github-mark" viewBox="0 0 16 16" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.67 0 8.2c0 3.62 2.29 6.69 5.47 7.77.4.08.55-.18.55-.4 0-.2-.01-.86-.01-1.56-2.01.38-2.53-.5-2.69-.95-.09-.23-.48-.95-.82-1.14-.28-.16-.68-.56-.01-.57.63-.01 1.08.59 1.23.83.72 1.25 1.87.9 2.33.68.07-.54.28-.9.51-1.11-1.78-.21-3.64-.91-3.64-4.05 0-.9.31-1.64.82-2.22-.08-.21-.36-1.05.08-2.18 0 0 .67-.22 2.2.85A7.37 7.37 0 0 1 8 4.68c.68 0 1.36.1 2 .29 1.53-1.07 2.2-.85 2.2-.85.44 1.13.16 1.97.08 2.18.51.58.82 1.31.82 2.22 0 3.15-1.87 3.84-3.65 4.05.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .22.15.49.55.4A8.23 8.23 0 0 0 16 8.2C16 3.67 12.42 0 8 0z"/>
    </svg>
    github.com/cohm/student-admin-bot
  </a>{version_html}
</div>
<p><a href="{home}" data-i18n="about.back"></a></p>
</div></main></body></html>
"""
    return HTMLResponse(body)


def _glossary_page(cfg: Config, base_path: str = "") -> HTMLResponse:
    j = Jargon.from_config(cfg)
    static_prefix = _join_base(base_path, "/static")
    home = _join_base(base_path, "/") or "/"
    jargon_api = _join_base(base_path, "/api/jargon/suggest")
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
<link rel="stylesheet" href="{static_prefix}/style.css?v=26">{_NOTICE_SCRIPT.format(static_prefix=static_prefix)}</head>
<body>{_HEADER_HTML.format(tagline_html='<p class="tagline" data-i18n="glossary.tagline"></p>', static_prefix=static_prefix)}
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
<p style="margin-top: 16px"><a href="{home}" data-i18n="glossary.back"></a></p>
</div></main>
<script>
async function submitJargon(e) {{
  e.preventDefault();
  const f = e.target;
  const status = document.getElementById('jargon-status');
  status.textContent = window.t ? window.t('glossary.status.sending') : 'skickar…';
  const r = await fetch('{jargon_api}', {{
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


def _md_doc_page(cfg: Config, docs_dir: Path, rel_source: str, base_path: str = "") -> HTMLResponse:
    """Render a curated `.md` file as a styled HTML page with attribution.

    Refuses anything outside `docs_dir`, anything under `web_import/`, and any
    non-`.md` path. The route should not see those because `_source_http_url`
    only emits render URLs for curated md, but we double-check here.
    """
    rs = (rel_source or "").strip().strip("/")
    if not rs.lower().endswith(".md") or rs.startswith("web_import/"):
        raise HTTPException(status_code=404, detail="Not a renderable document")

    abs_path = (docs_dir / rs).resolve()
    try:
        abs_path.relative_to(docs_dir)
    except ValueError as e:
        raise HTTPException(status_code=403, detail="Path outside corpus") from e
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="Document not found")

    doc = render_file(abs_path)
    static_prefix = _join_base(base_path, "/static")
    home = _join_base(base_path, "/") or "/"
    raw_link = _join_base(base_path, cfg.web.doc_base_url.rstrip("/") + "/" + rs)

    def _join_authors(joiner: str) -> str:
        parts: list[str] = []
        for a in doc.authors:
            label = _h(a.name)
            if a.role:
                label += f" ({_h(a.role)})"
            parts.append(label)
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return joiner.join([", ".join(parts[:-1]), parts[-1]])

    sv_authors = _join_authors(" och ")
    en_authors = _join_authors(" and ")
    upd = _h(doc.updated)
    attribution_html = ""
    if sv_authors or upd:
        sv_bits: list[str] = []
        en_bits: list[str] = []
        if sv_authors:
            sv_bits.append(f"Sammanställt av <strong>{sv_authors}</strong>")
            en_bits.append(f"Compiled by <strong>{en_authors}</strong>")
        if upd:
            sv_bits.append(f"senast uppdaterad {upd}")
            en_bits.append(f"last updated {upd}")
        sv_text = ", ".join(sv_bits) + "."
        en_text = ", ".join(en_bits) + "."
        attribution_html = (
            '<footer class="md-attribution">'
            f'<span class="lang-sv">{sv_text}</span>'
            f'<span class="lang-en">{en_text}</span>'
            "</footer>"
        )

    body = f"""
<!doctype html><html lang="sv"><head><meta charset="utf-8"><title>{_h(doc.title)}</title>
<link rel="stylesheet" href="{static_prefix}/style.css?v=26">{_NOTICE_SCRIPT.format(static_prefix=static_prefix)}</head>
<body>{_HEADER_HTML.format(tagline_html="", static_prefix=static_prefix)}
<main><div class="card md-doc">
<nav class="md-nav">
  <a href="{home}"><span class="lang-sv">← Tillbaka till chatten</span><span class="lang-en">← Back to the chat</span></a>
  · <a href="{raw_link}"><span class="lang-sv">Rå källfil</span><span class="lang-en">Raw source file</span></a>
</nav>
<article class="md-body">
{doc.body_html}
</article>
{attribution_html}
</div></main></body></html>
"""
    return HTMLResponse(body)


_STATS_RANGES: dict[str, tuple[int, int]] = {
    # range key -> (bucket_seconds, span_seconds)
    "24h": (900, 86400),
    "72h": (3600, 72 * 3600),
    "14d": (6 * 3600, 14 * 86400),
    "90d": (86400, 90 * 86400),
}
_STATS_RANGE_KEYS = ("24h", "72h", "14d", "90d")

_STATS_CHANNELS = ("all", "web", "mm")


def _normalize_channel(value: str | None) -> str:
    if value not in _STATS_CHANNELS:
        return "all"
    return value


def _densify_buckets(
    buckets: list[dict], since_ts: int, now_ts: int, bucket_seconds: int
) -> list[dict]:
    """Fill bucket gaps with zero-rows so the chart x-axis is a real timeline.

    SQL only emits buckets that have rows; for plotting we want every
    bucket_seconds slot from the first window-aligned bucket up to (and
    including) the bucket containing `now_ts`. Bucket alignment matches the
    SQL: `(ts // bucket_seconds) * bucket_seconds`.
    """
    start = (since_ts // bucket_seconds) * bucket_seconds
    end = (now_ts // bucket_seconds) * bucket_seconds
    by_ts = {b["bucket_ts"]: b for b in buckets}
    out: list[dict] = []
    t = start
    while t <= end:
        if t in by_ts:
            out.append(by_ts[t])
        else:
            out.append(
                {
                    "bucket_ts": t,
                    "n": 0,
                    "n_answered": 0,
                    "prompt_tokens": 0,
                    "gen_tokens": 0,
                    "thumbs_up": 0,
                    "thumbs_down": 0,
                }
            )
        t += bucket_seconds
    return out


def _stats_series_response(db: LogDB, range_key: str, channel: str = "all") -> JSONResponse:
    spec = _STATS_RANGES.get(range_key)
    if spec is None:
        raise HTTPException(400, f"unknown range '{range_key}'")
    bucket_seconds, span_seconds = spec
    now = int(time.time())
    since = now - span_seconds
    raw = db.series_buckets(since, bucket_seconds, channel=channel)
    return JSONResponse(
        {
            "range": range_key,
            "channel": channel,
            "bucket_seconds": bucket_seconds,
            "since_ts": since,
            "now_ts": now,
            "buckets": _densify_buckets(raw, since, now, bucket_seconds),
        }
    )


def _format_relative_ts(now: int, ts: int) -> str:
    """Compact relative time (e.g. '3h', '2d') for the user-activity table."""
    if ts <= 0:
        return "—"
    delta = max(0, now - ts)
    if delta < 60:
        return "<1m"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    if delta < 30 * 86400:
        return f"{delta // 86400}d"
    return f"{delta // (30 * 86400)}mo"


def _stats_page(
    cfg: Config,
    db: LogDB,
    base_path: str = "",
    *,
    channel: str = "all",
) -> HTMLResponse:
    overall = db.overall_counts(channel=channel)
    by_topic = db.stats_by_topic(channel=channel)
    static_prefix = _join_base(base_path, "/static")
    home = _join_base(base_path, "/") or "/"
    stats_path = _join_base(base_path, "/stats") or "/stats"

    # Registered web users → cross-reference qa_log via the same hash the bot
    # already uses. No new schema; activity is derived per request. The
    # section is web-specific by definition, so it's hidden when filtering to
    # Mattermost-only.
    show_users = channel != "mm"
    registered = list_usernames(cfg) if show_users else []
    hash_to_user = {db.hash_user(f"basic:{u}"): u for u in registered}
    activity = db.activity_for_users(list(hash_to_user.keys())) if hash_to_user else {}
    now_ts = int(time.time())
    user_rows_data = [
        {
            "username": hash_to_user[h],
            "n_qa": a["n_qa"],
            "last_ts": a["last_ts"],
        }
        for h, a in activity.items()
    ]
    user_rows_data.sort(key=lambda r: (-r["n_qa"], r["username"]))
    if user_rows_data:
        user_rows = "".join(
            f'<tr><td class="text">{_h(r["username"])}</td>'
            f'<td class="num">{r["n_qa"]}</td>'
            f'<td class="num">{_format_relative_ts(now_ts, r["last_ts"])}</td></tr>'
            for r in user_rows_data
        )
    else:
        user_rows = '<tr><td colspan="3" class="text" data-i18n="stats.empty"></td></tr>'
    n_active = len(user_rows_data)
    n_total = len(registered)

    if by_topic:
        topic_rows = "".join(
            f'<tr><td class="text">{_h(r["topic"])}</td>'
            f'<td class="num">{r["n"]}</td>'
            f'<td class="num">{r["answered"]}</td>'
            f'<td class="num">{r["avg_latency_ms"]}</td>'
            f'<td class="num">{r["thumbs_up"]}</td>'
            f'<td class="num">{r["thumbs_down"]}</td></tr>'
            for r in by_topic
        )
    else:
        topic_rows = '<tr><td colspan="6" class="text" data-i18n="stats.empty"></td></tr>'

    def _ch_link(value: str) -> str:
        href = stats_path if value == "all" else f"{stats_path}?ch={value}"
        active = " active" if channel == value else ""
        return (
            f'<a class="stats-channel-btn{active}" href="{href}" '
            f'data-i18n="stats.channel.{value}"></a>'
        )

    channel_switch_html = (
        '<div class="stats-channel" role="group" '
        'aria-label="Channel filter">'
        + _ch_link("all")
        + _ch_link("web")
        + _ch_link("mm")
        + "</div>"
    )

    if show_users:
        users_section = f"""
<section class="stats-users">
  <h2 data-i18n="stats.users.title"></h2>
  <p class="stats-users-summary" data-i18n="stats.users.summary"
     data-active="{n_active}" data-total="{n_total}"></p>
  <table class="stats-table stats-users-table">
    <thead><tr>
      <th class="text" data-i18n="stats.users.th.username"></th>
      <th class="num" data-i18n="stats.users.th.n"></th>
      <th class="num" data-i18n="stats.users.th.last"></th>
    </tr></thead>
    <tbody>{user_rows}</tbody>
  </table>
</section>
"""
    else:
        users_section = ""

    body = f"""
<!doctype html><html lang="sv"><head><meta charset="utf-8"><title>student-bot</title>
<link rel="stylesheet" href="{static_prefix}/style.css?v=26">{_NOTICE_SCRIPT.format(static_prefix=static_prefix)}</head>
<body>{_HEADER_HTML.format(tagline_html="", static_prefix=static_prefix)}<main>{_NOTICE_HTML}<div class="card stats-card" data-channel="{channel}">
<h1 data-i18n="stats.title"></h1>
{channel_switch_html}
<p class="stats-summary" data-i18n="stats.summary"
   data-logged="{overall["logged"]}"
   data-answered="{overall["answered"]}"
   data-latency="{overall["avg_latency_ms"]}"
   data-anon="{overall["anon"]}"></p>

<section class="stats-charts">
  <div class="stats-controls">
    <div class="stats-ranges" role="group">
      <button type="button" data-range="24h" data-i18n="stats.range.24h"></button>
      <button type="button" data-range="72h" data-i18n="stats.range.72h"></button>
      <button type="button" data-range="14d" data-i18n="stats.range.14d"></button>
      <button type="button" data-range="90d" data-i18n="stats.range.90d"></button>
    </div>
    <label class="stats-logy">
      <input type="checkbox" id="stats-logy">
      <span data-i18n="stats.chart.logy"></span>
    </label>
  </div>
  <div class="stats-chart-head">
    <h3 data-i18n="stats.chart.requests"></h3>
    <button type="button" class="stats-export" data-export-chart="requests"
            data-i18n="stats.export.png" data-i18n-title="stats.export.png.title"></button>
  </div>
  <canvas id="chart-requests" height="220"></canvas>
  <div class="stats-chart-head">
    <h3 data-i18n="stats.chart.tokens"></h3>
    <button type="button" class="stats-export" data-export-chart="tokens"
            data-i18n="stats.export.png" data-i18n-title="stats.export.png.title"></button>
  </div>
  <canvas id="chart-tokens" height="220"></canvas>
  <div class="stats-chart-head">
    <h3 data-i18n="stats.chart.feedback"></h3>
    <button type="button" class="stats-export" data-export-chart="feedback"
            data-i18n="stats.export.png" data-i18n-title="stats.export.png.title"></button>
  </div>
  <canvas id="chart-feedback" height="160"></canvas>
</section>
{users_section}
<section class="stats-topics">
  <h2 data-i18n="stats.topics.title"></h2>
  <table class="stats-table stats-topics-table">
    <thead><tr>
      <th class="text" data-i18n="stats.th.topic"></th>
      <th class="num" data-i18n="stats.th.n"></th>
      <th class="num" data-i18n="stats.th.answered"></th>
      <th class="num" data-i18n="stats.th.avgms"></th>
      <th class="num">👍</th>
      <th class="num">👎</th>
    </tr></thead>
    <tbody>{topic_rows}</tbody>
  </table>
</section>

<p><a href="{home}" data-i18n="stats.back"></a></p>
</div></main>
<script src="{static_prefix}/vendor/chart.umd.min.js?v=26"></script>
<script src="{static_prefix}/stats.js?v=26" defer></script>
</body></html>
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

    # Uvicorn access logs get noisy when the performance panel is enabled:
    # the browser polls /api/system-load at 1 Hz. Suppress just that endpoint
    # unless explicitly disabled by env.
    if os.environ.get("WEB_SUPPRESS_SYSTEM_LOAD_ACCESS_LOG", "1") != "0":

        class _SuppressSystemLoadAccessLog(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 (filter is stdlib name)
                msg = record.getMessage()
                return "GET /api/system-load " not in msg

        logging.getLogger("uvicorn.access").addFilter(_SuppressSystemLoadAccessLog())

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
