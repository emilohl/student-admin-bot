"""Full RAG flow: lang-detect → guardrails → retrieve → gate → generate (or refuse).

Programmatic API (used by Mattermost client and web app):
    answer(question, history=[], rate_limit_key=None) -> AnswerResult

CLI:
    student-bot-cli "Hur överklagar jag ett betyg?"
    student-bot-cli --interactive
"""
from __future__ import annotations

import sys
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from threading import Lock

import click
from rich.console import Console

from student_bot.bot.citations import (
    confidence_badge,
    format_sources_block,
    literacy_footer,
)
from student_bot.bot.gate import GateDecision, evaluate as evaluate_gate
from student_bot.bot.llm import stream_chat
from student_bot.bot.memory import ConversationMemory
from student_bot.bot.prompts import compose_messages, refusal_message
from student_bot.bot.retrieval import RetrievalResult, RetrievedChunk, retrieve
from student_bot.config import Config, get_config
from student_bot.lang import detect


@dataclass
class AnswerResult:
    question: str
    lang: str
    answered: bool
    answer: str                  # the model's text only (no sources/footer)
    rendered: str                # answer + sources block + footer (what to display)
    gate: GateDecision
    retrieval: RetrievalResult
    latency_ms: int
    rate_limited: bool = False
    too_long: bool = False


# --- Per-key rate limiter (simple sliding window over the last 60 s) ---


@dataclass
class _RateLimiter:
    cfg: Config
    _hits: dict[str, deque] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def allow(self, key: str) -> bool:
        if not key:
            return True
        limit = self.cfg.guardrails.rate_limit_per_minute
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and now - dq[0] > 60:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True


_rate_limiters: dict[int, _RateLimiter] = {}


def _rate_limiter(cfg: Config) -> _RateLimiter:
    key = id(cfg)
    rl = _rate_limiters.get(key)
    if rl is None:
        rl = _RateLimiter(cfg)
        _rate_limiters[key] = rl
    return rl


def _too_long_message(cfg: Config, lang: str) -> str:
    cap = cfg.guardrails.input_max_chars
    if lang == "en":
        return (
            f"Your message is too long ({cap} character limit). "
            "Please ask a shorter, more focused question."
        )
    return (
        f"Frågan är för lång (max {cap} tecken). "
        "Ställ en kortare, mer fokuserad fråga."
    )


def _rate_limited_message(cfg: Config, lang: str) -> str:
    n = cfg.guardrails.rate_limit_per_minute
    if lang == "en":
        return f"Slow down — you can ask up to {n} questions per minute."
    return f"Lugna ner dig lite — högst {n} frågor per minut."


def _render(
    cfg: Config,
    lang: str,
    body: str,
    chunks: list[RetrievedChunk],
    gate: GateDecision,
    *,
    include_sources: bool,
) -> str:
    parts: list[str] = []
    if cfg.guardrails.show_confidence_badge and include_sources:
        label = "Tillförlitlighet" if lang == "sv" else "Confidence"
        parts.append(f"_{label}: {confidence_badge(lang, gate.top1)}_\n")
    parts.append(body)
    if include_sources:
        sources = format_sources_block(cfg, chunks, lang)
        if sources:
            parts.append(sources)
    parts.append("\n\n" + literacy_footer(lang))
    return "".join(parts).strip()


def answer(
    question: str,
    history: list[dict] | None = None,
    cfg: Config | None = None,
    on_token=None,
    rate_limit_key: str | None = None,
) -> AnswerResult:
    cfg = cfg or get_config()
    history = history or []
    t0 = time.monotonic()

    # --- guardrails: input length cap and per-user rate limit ---
    lang = detect(question)
    if cfg.guardrails.input_max_chars and len(question) > cfg.guardrails.input_max_chars:
        msg = _too_long_message(cfg, lang)
        if on_token:
            on_token(msg)
        return AnswerResult(
            question=question, lang=lang, answered=False,
            answer=msg, rendered=msg,
            gate=GateDecision(False, "input_too_long", 0.0, 0.0, 0),
            retrieval=RetrievalResult(query=question),
            latency_ms=int((time.monotonic() - t0) * 1000),
            too_long=True,
        )
    if rate_limit_key and not _rate_limiter(cfg).allow(rate_limit_key):
        msg = _rate_limited_message(cfg, lang)
        if on_token:
            on_token(msg)
        return AnswerResult(
            question=question, lang=lang, answered=False,
            answer=msg, rendered=msg,
            gate=GateDecision(False, "rate_limited", 0.0, 0.0, 0),
            retrieval=RetrievalResult(query=question),
            latency_ms=int((time.monotonic() - t0) * 1000),
            rate_limited=True,
        )

    retrieval = retrieve(cfg, question)
    gate = evaluate_gate(cfg, retrieval)

    if not gate.passed:
        body = refusal_message(cfg, lang)
        rendered = _render(cfg, lang, body, [], gate, include_sources=False)
        if on_token:
            on_token(rendered)
        return AnswerResult(
            question=question, lang=lang, answered=False,
            answer=body, rendered=rendered,
            gate=gate, retrieval=retrieval,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    messages = compose_messages(cfg, lang, history, retrieval.reranked, question)
    parts: list[str] = []
    for delta in _stream_answer(cfg, messages):
        parts.append(delta)
        if on_token:
            on_token(delta)
    body = "".join(parts).strip()
    rendered = _render(cfg, lang, body, retrieval.reranked, gate, include_sources=True)
    # Sources & footer are appended AFTER streaming completes; emit them as
    # a final delta so streaming UIs see the same text the API returns.
    if on_token:
        tail = rendered[len(body):]
        if tail:
            on_token(tail)

    return AnswerResult(
        question=question, lang=lang, answered=True,
        answer=body, rendered=rendered,
        gate=gate, retrieval=retrieval,
        latency_ms=int((time.monotonic() - t0) * 1000),
    )


def _stream_answer(cfg: Config, messages: list[dict]) -> Iterator[str]:
    yield from stream_chat(cfg, messages)


# --- CLI ---


@click.command()
@click.argument("question", nargs=-1, required=False)
@click.option("--show-context", is_flag=True, help="Print retrieved chunks before the answer.")
@click.option("--no-stream", is_flag=True, help="Wait for full response instead of streaming.")
@click.option(
    "-i", "--interactive", is_flag=True,
    help="REPL mode: keeps short conversation memory between turns.",
)
def main(question: tuple[str, ...], show_context: bool, no_stream: bool, interactive: bool):
    cfg = get_config()
    console = Console()

    if interactive:
        _repl(cfg, console, show_context=show_context)
        return

    if not question:
        console.print("[red]Provide a question, or use --interactive.[/red]")
        sys.exit(2)

    q = " ".join(question)
    _run_once(cfg, console, q, show_context=show_context, no_stream=no_stream)


def _run_once(cfg: Config, console: Console, q: str, *, show_context: bool, no_stream: bool):
    if show_context:
        from student_bot.bot.retrieval import retrieve as _rt
        r = _rt(cfg, q)
        _print_context(console, r.reranked)

    if no_stream:
        result = answer(q, cfg=cfg)
        console.print(result.rendered)
    else:
        printed_any = False

        def on_tok(delta: str):
            nonlocal printed_any
            sys.stdout.write(delta)
            sys.stdout.flush()
            printed_any = True

        result = answer(q, cfg=cfg, on_token=on_tok)
        if printed_any:
            sys.stdout.write("\n")

    console.print()
    console.print(
        f"[dim]lang={result.lang}  answered={result.answered}  "
        f"gate={result.gate.reason}  top1={result.gate.top1:.3f}  "
        f"meanK={result.gate.meanK:.3f}  sources={result.gate.distinct_sources}  "
        f"latency={result.latency_ms}ms[/dim]"
    )


def _repl(cfg: Config, console: Console, *, show_context: bool):
    """REPL mode — same conversation memory model the bot uses for threads."""
    memory = ConversationMemory(cfg)
    console.print("[bold]student-bot[/bold] interactive mode. Empty line or :q to exit.")
    user_id = "cli"
    thread_id = "default"
    while True:
        try:
            q = console.input("[bold cyan]› [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not q or q in (":q", ":quit", "/quit", "exit"):
            break
        if q in (":reset", ":clear"):
            memory.clear(user_id, thread_id)
            console.print("[dim]conversation memory cleared[/dim]")
            continue

        history = memory.get(user_id, thread_id)
        if show_context:
            from student_bot.bot.retrieval import retrieve as _rt
            r = _rt(cfg, q)
            _print_context(console, r.reranked)

        printed_any = False

        def on_tok(delta: str):
            nonlocal printed_any
            sys.stdout.write(delta)
            sys.stdout.flush()
            printed_any = True

        result = answer(q, history=history, cfg=cfg, on_token=on_tok)
        if printed_any:
            sys.stdout.write("\n")

        if result.answered:
            memory.append(user_id, thread_id, "user", q)
            memory.append(user_id, thread_id, "assistant", result.answer)

        console.print(
            f"[dim]lang={result.lang}  gate={result.gate.reason}  "
            f"top1={result.gate.top1:.3f}  latency={result.latency_ms}ms[/dim]\n"
        )


def _print_context(console: Console, chunks: list[RetrievedChunk]):
    console.print("[bold cyan]Retrieved context:[/bold cyan]")
    for i, c in enumerate(chunks, 1):
        section = c.section_path or "—"
        page = f" p.{c.page_start}" if c.page_start else ""
        preview = c.text.strip().replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:200] + "…"
        console.print(
            f"  [bold]{i}.[/bold] [{c.doc_title} · {section}{page}] "
            f"[dim](score={c.rerank_score:.3f}, dist={c.chroma_distance:.3f})[/dim]\n"
            f"     {preview}"
        )
    console.print()


if __name__ == "__main__":
    main()


__all__ = ["AnswerResult", "answer"]
