"""Ollama client wrapper. Streaming chat completion with optional
filtering of Gemma 4 reasoning blocks.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from functools import lru_cache

from ollama import Client

from student_bot.config import Config


log = logging.getLogger("student_bot")


@lru_cache(maxsize=1)
def _client(url: str) -> Client:
    return Client(host=url)


def get_client(cfg: Config) -> Client:
    return _client(cfg.llm.ollama_url)


# Gemma 4 wraps its internal reasoning in a `<|channel>thought\n` ...
# `<channel|>` block (per the Unsloth model card) when the `<|think|>`
# token is present in the system prompt. Note the asymmetric markers —
# the open tag contains a trailing newline, the close tag does not.
_OPEN_TAG = "<|channel>thought\n"
_CLOSE_TAG = "<channel|>"


def stream_chat(
    cfg: Config,
    messages: list[dict],
    *,
    on_thinking: Callable[[bool], None] | None = None,
) -> Iterator[str]:
    """Yield response text deltas. Caller joins.

    Filters <think>...</think> blocks from the stream so reasoning content
    never reaches downstream consumers. If `on_thinking` is provided, it's
    called with True at the start of a think block and False at the end —
    callers can use that to surface a "thinking…" indicator.
    """
    client = get_client(cfg)
    options = {
        "num_ctx": cfg.llm.num_ctx,
        "temperature": cfg.llm.temperature,
        "num_predict": cfg.llm.max_tokens,
    }
    # Newer ollama-python supports a `think=True` request parameter for
    # reasoning-capable models (Qwen3, DeepSeek-R1, GPT-OSS, Gemma 4).
    # Try it first; if the installed client rejects it, retry without.
    chat_kwargs = dict(model=cfg.llm.model, messages=messages, stream=True, options=options)
    if cfg.llm.thinking:
        try:
            raw = client.chat(**chat_kwargs, think=True)
        except TypeError:
            log.info("ollama-python does not accept think=True; relying on <|think|> token only")
            raw = client.chat(**chat_kwargs)
    else:
        raw = client.chat(**chat_kwargs)

    in_think = False
    buf = ""
    raw_capture: list[str] = []  # diagnostic — see end of stream

    saw_thinking = False
    api_thinking_phase = False  # tracks the ollama `thinking` field state

    for chunk in raw:
        msg = chunk.get("message", {})
        thinking_delta = msg.get("thinking") or ""
        delta = msg.get("content") or ""

        # Ollama API thinking mode: reasoning arrives as a separate
        # `thinking` field on the message, with `content` empty until
        # the model transitions to its final answer. Fire the on_thinking
        # callback on each phase transition so the UI can show/hide its
        # "<bot> funderar…" label.
        if thinking_delta:
            if not api_thinking_phase:
                api_thinking_phase = True
                saw_thinking = True
                if on_thinking:
                    on_thinking(True)
            # Discard the thinking content; never yield it downstream.
            continue
        if api_thinking_phase and delta:
            api_thinking_phase = False
            if on_thinking:
                on_thinking(False)

        if not delta:
            continue
        if cfg.llm.thinking:
            raw_capture.append(delta)
        buf += delta

        # Process the buffer as far as possible without consuming a partial
        # tag (which we'd then misinterpret on the next chunk).
        while True:
            if not in_think:
                idx = buf.find(_OPEN_TAG)
                if idx >= 0:
                    if idx > 0:
                        yield buf[:idx]
                    buf = buf[idx + len(_OPEN_TAG):]
                    in_think = True
                    saw_thinking = True
                    if on_thinking:
                        on_thinking(True)
                    continue
                # No open tag yet. Emit everything except any trailing
                # bytes that could still grow into "<think>".
                last_lt = buf.rfind("<")
                if last_lt == -1:
                    if buf:
                        yield buf
                    buf = ""
                else:
                    suffix = buf[last_lt:]
                    if _OPEN_TAG.startswith(suffix):
                        if last_lt > 0:
                            yield buf[:last_lt]
                        buf = suffix
                    else:
                        yield buf
                        buf = ""
                break
            else:
                idx = buf.find(_CLOSE_TAG)
                if idx >= 0:
                    buf = buf[idx + len(_CLOSE_TAG):]
                    in_think = False
                    if on_thinking:
                        on_thinking(False)
                    continue
                # No close tag yet. Bound memory by keeping only the trailing
                # bytes that could still grow into "</think>".
                hold = len(_CLOSE_TAG) - 1
                if len(buf) > hold:
                    buf = buf[-hold:]
                break

    # Flush any remaining buffer (only if we left/never entered a think block).
    if not in_think and buf:
        yield buf

    # Diagnostic: when thinking is configured on but we never saw any
    # reasoning content (neither tag-based nor the ollama `thinking`
    # field), log a warning with a snippet so we can see what the model
    # actually emitted. Common causes: the GGUF chat template doesn't
    # honor the <|think|> system-prompt token, or the model wasn't
    # configured with the right Modelfile parameters.
    if cfg.llm.thinking and not saw_thinking:
        sample = "".join(raw_capture)[:200]
        log.warning(
            "thinking enabled but model emitted no reasoning tags. "
            "Raw stream start: %r", sample,
        )


def chat(cfg: Config, messages: list[dict]) -> str:
    return "".join(stream_chat(cfg, messages))


__all__ = ["stream_chat", "chat", "get_client"]
