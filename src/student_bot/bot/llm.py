"""LLM streaming wrapper. Dispatches between the local Ollama client and
an OpenAI-compatible cloud provider (Berget AI, OpenRouter, Together, …)
based on the active model's `provider_kind`.

The active model is selected by `cfg.llm.active` and resolved via
`cfg.active_model() -> ResolvedLLM`. Each call site can also pass
`temperature_override` / `max_tokens_override` for off-path uses like the
topic classifier (lower temperature, shorter output).

Both paths emit response-text deltas via the same generator interface and
filter reasoning content so it never reaches downstream consumers. The
filter behaviour is controlled by `ResolvedLLM.thinking_style`:
- `"gemma"`               — Gemma 4's `<|channel>thought\\n...<channel|>` block
                            plus the ollama API's `thinking` field.
- `"openai_reasoning_field"` — DeepSeek-R1 / Qwen-style `delta.reasoning_content`
                            (or `delta.reasoning`) alongside `delta.content`.
- `"none"`                — model has no reasoning channel; pass through.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from functools import lru_cache

import httpx
from ollama import Client

from student_bot.config import Config, ResolvedLLM


log = logging.getLogger("student_bot")


@lru_cache(maxsize=4)
def _client(url: str) -> Client:
    return Client(host=url)


def get_client(cfg: Config) -> Client:
    """Returns an Ollama client targeting the currently-active model's
    provider (when the active provider is Ollama). Raises if the active
    provider isn't Ollama — callers that need a raw client should resolve
    explicitly via `cfg.active_model()` and check `provider_kind`.
    """
    resolved = cfg.active_model()
    if resolved.provider_kind != "ollama":
        raise RuntimeError(
            f"get_client() requires the active provider to be ollama, "
            f"got {resolved.provider_kind!r} (active={resolved.identifier!r})"
        )
    return _client(resolved.base_url)


# Gemma 4 wraps its internal reasoning in a `<|channel>thought\n` ...
# `<channel|>` block (per the Unsloth model card) when the `<|think|>`
# token is present in the system prompt. Note the asymmetric markers —
# the open tag contains a trailing newline, the close tag does not.
_OPEN_TAG = "<|channel>thought\n"
_CLOSE_TAG = "<channel|>"


def _filter_gemma_channel_blocks(
    deltas: Iterator[str],
    on_thinking: Callable[[bool], None] | None = None,
) -> Iterator[str]:
    """Strip `<|channel>thought\\n...<channel|>` blocks from a content
    stream. Works for both Ollama-served and OpenAI-compatible-served
    Gemma models since the marker tokens are emitted by the model itself
    (in `delta.content`), independent of transport.

    Calls `on_thinking(True)` when entering a block and `on_thinking(False)`
    when leaving — callers can use that to surface a "thinking…" indicator.
    Reasoning text is never yielded. The marker tokens can span chunk
    boundaries (e.g. `<|` arrives in one delta, `channel>thought\\n` in
    the next), so we buffer up to one closing-tag-length of trailing bytes
    that could still grow into the open or close tag.
    """
    in_think = False
    buf = ""
    for delta in deltas:
        if not delta:
            continue
        buf += delta
        # Process the buffer as far as possible without consuming a partial
        # tag (which we'd misinterpret on the next chunk).
        while True:
            if not in_think:
                idx = buf.find(_OPEN_TAG)
                if idx >= 0:
                    if idx > 0:
                        yield buf[:idx]
                    buf = buf[idx + len(_OPEN_TAG) :]
                    in_think = True
                    if on_thinking:
                        on_thinking(True)
                    continue
                # No open tag yet. Emit everything except any trailing
                # bytes that could still grow into the open marker.
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
                    buf = buf[idx + len(_CLOSE_TAG) :]
                    in_think = False
                    if on_thinking:
                        on_thinking(False)
                    continue
                # No close tag yet. Bound memory by keeping only the trailing
                # bytes that could still grow into the close marker.
                hold = len(_CLOSE_TAG) - 1
                if len(buf) > hold:
                    buf = buf[-hold:]
                break
    # Flush any remaining buffer only if we exited the think block.
    if not in_think and buf:
        yield buf


def stream_chat(
    cfg: Config,
    messages: list[dict],
    *,
    on_thinking: Callable[[bool], None] | None = None,
    temperature_override: float | None = None,
    max_tokens_override: int | None = None,
) -> Iterator[str]:
    """Yield response text deltas. Caller joins.

    Resolves `cfg.active_model()` once and dispatches to the right backend.
    Optional `temperature_override` / `max_tokens_override` let off-path
    callers (e.g. the topic classifier) tweak sampling without mutating
    the registered model config.
    """
    resolved = cfg.active_model()
    temperature = temperature_override if temperature_override is not None else resolved.temperature
    max_tokens = max_tokens_override if max_tokens_override is not None else resolved.max_tokens
    if resolved.provider_kind == "ollama":
        yield from _stream_chat_ollama(
            resolved,
            messages,
            on_thinking=on_thinking,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return
    if resolved.provider_kind == "openai_compatible":
        yield from _stream_chat_openai(
            resolved,
            messages,
            on_thinking=on_thinking,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return
    raise RuntimeError(
        f"unknown provider_kind={resolved.provider_kind!r} for active model "
        f"{resolved.identifier!r}; expected 'ollama' or 'openai_compatible'"
    )


def _stream_chat_ollama(
    resolved: ResolvedLLM,
    messages: list[dict],
    *,
    on_thinking: Callable[[bool], None] | None = None,
    temperature: float,
    max_tokens: int,
) -> Iterator[str]:
    """Local-Ollama streaming path.

    Two reasoning surfaces are stripped before yielding:
      1. The ollama API's separate `message.thinking` field — handled inline
         here; reasoning text never reaches `delta.content`.
      2. When `thinking_style == "gemma"`, the in-content
         `<|channel>thought\\n...<channel|>` block emitted by Gemma 4 itself.
         Wrapped via `_filter_gemma_channel_blocks` so the same filter also
         applies on the OpenAI-compatible path when that model is served
         remotely.
    """
    client = _client(resolved.base_url)
    options = {
        "num_ctx": resolved.num_ctx,
        "temperature": temperature,
        "num_predict": max_tokens,
    }
    # Newer ollama-python supports a `think=True` request parameter for
    # reasoning-capable models (Qwen3, DeepSeek-R1, GPT-OSS, Gemma 4).
    # Try it first; if the installed client rejects it, retry without.
    chat_kwargs = dict(model=resolved.model_id, messages=messages, stream=True, options=options)
    gemma_style = resolved.thinking_style == "gemma" and resolved.thinking
    if gemma_style:
        try:
            raw = client.chat(**chat_kwargs, think=True)
        except TypeError:
            log.info("ollama-python does not accept think=True; relying on <|think|> token only")
            raw = client.chat(**chat_kwargs)
    else:
        raw = client.chat(**chat_kwargs)

    # Track whether we ever observed reasoning content for the diagnostic
    # warning at the end. Updated by both the ollama-field handler below
    # and the channel-block wrapper via `track_on_thinking`.
    state = {"saw_thinking": False, "raw_capture": []}

    def track_on_thinking(active: bool) -> None:
        if active:
            state["saw_thinking"] = True
        if on_thinking:
            on_thinking(active)

    def _ollama_content_only() -> Iterator[str]:
        """Yield only `message.content` deltas. Routes `message.thinking`
        deltas to `track_on_thinking` without yielding them."""
        api_thinking_phase = False
        for chunk in raw:
            msg = chunk.get("message", {})
            thinking_delta = msg.get("thinking") or ""
            delta = msg.get("content") or ""

            # Ollama API thinking mode: reasoning arrives as a separate
            # `thinking` field on the message; `content` is empty until the
            # model transitions to its final answer.
            if thinking_delta:
                if not api_thinking_phase:
                    api_thinking_phase = True
                    track_on_thinking(True)
                continue
            if api_thinking_phase and delta:
                api_thinking_phase = False
                track_on_thinking(False)

            if not delta:
                continue
            if gemma_style:
                state["raw_capture"].append(delta)
            yield delta
        # If we exit mid-thinking-phase (shouldn't happen with ollama, but
        # be defensive), signal the close so UI indicators don't stick.
        if api_thinking_phase:
            track_on_thinking(False)

    content_stream = _ollama_content_only()
    if gemma_style:
        yield from _filter_gemma_channel_blocks(content_stream, on_thinking=track_on_thinking)
    else:
        yield from content_stream

    # Diagnostic: when Gemma thinking is configured on but we never saw any
    # reasoning content (neither tag-based nor the ollama `thinking` field),
    # log a warning with a snippet so we can see what the model actually
    # emitted. Common causes: the GGUF chat template doesn't honor the
    # <|think|> system-prompt token, or the model wasn't configured with
    # the right Modelfile parameters.
    if gemma_style and not state["saw_thinking"]:
        sample = "".join(state["raw_capture"])[:200]
        log.warning(
            "thinking enabled but model emitted no reasoning tags. Raw stream start: %r",
            sample,
        )


def _stream_chat_openai(
    resolved: ResolvedLLM,
    messages: list[dict],
    *,
    on_thinking: Callable[[bool], None] | None = None,
    temperature: float,
    max_tokens: int,
) -> Iterator[str]:
    """OpenAI-compatible SSE streaming. Works with Berget AI, OpenRouter,
    Together, Groq, Fireworks, OpenAI itself.

    Reasoning is filtered out before yielding via two mechanisms, picked
    based on the active model's `thinking_style`:

    - `"openai_reasoning_field"`: reasoning arrives in a separate JSON field
      (`delta.reasoning_content` or `delta.reasoning`) — strip the field,
      route phase changes to `on_thinking`.
    - `"gemma"`: reasoning is embedded *inside* `delta.content` as
      `<|channel>thought\\n...<channel|>` blocks. Wrap the content-delta
      stream through `_filter_gemma_channel_blocks` (shared with the local
      Ollama path) so the same Gemma model behaves consistently regardless
      of transport.
    - `"none"`: pass `delta.content` through, but still strip the
      `reasoning_content` field defensively in case a provider sends it.
    """
    base_url = (resolved.base_url or "").rstrip("/")
    if not base_url:
        raise RuntimeError(
            f"provider {resolved.provider_key!r} has no base_url; set it in "
            "config.yaml under llm.providers"
        )
    # The API key SecretStr stays redacted in cfg repr/dumps. Pull the raw
    # bearer token only at the request site, never store it in a
    # longer-lived scope.
    secret = resolved.api_key
    api_key = secret.get_secret_value() if secret is not None else ""
    if not api_key:
        raise RuntimeError(
            f"no API key loaded for provider {resolved.provider_key!r}; "
            f"set the env var named by llm.providers.{resolved.provider_key}.api_key_env"
        )
    url = f"{base_url}/chat/completions"
    payload = {
        "model": resolved.model_id,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    state = {
        "saw_any_content": False,
        "saw_thinking": False,
        "raw_capture": [],
        "in_thinking": False,
    }
    gemma_style = resolved.thinking_style == "gemma"

    def track_on_thinking(active: bool) -> None:
        state["in_thinking"] = active
        if active:
            state["saw_thinking"] = True
        if on_thinking:
            on_thinking(active)

    def _raw_content_deltas() -> Iterator[str]:
        """Yield only `delta.content`. Routes `delta.reasoning_content` /
        `delta.reasoning` (DeepSeek-R1 / Qwen convention) to
        `track_on_thinking` without yielding it. Gemma-channel filtering is
        applied as a post-process wrapper outside this generator."""
        in_reasoning_field = False
        try:
            with httpx.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
                timeout=resolved.timeout_seconds,
            ) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", errors="replace")[:500]
                    # Belt-and-suspenders: in the unlikely event a provider
                    # echoes the auth header back in its error body, redact
                    # it before raising — the RuntimeError surface ends up
                    # in user-facing error messages and logs.
                    if api_key and api_key in body:
                        body = body.replace(api_key, "<redacted>")
                    raise RuntimeError(f"cloud LLM HTTP {resp.status_code} from {url}: {body}")
                for line in resp.iter_lines():
                    if not line:
                        continue
                    # httpx.iter_lines yields strings (no `\n`). Each SSE
                    # event is a `data: …` line; control events and comments
                    # start with `:` or other prefixes — skip those.
                    if not line.startswith("data:"):
                        continue
                    payload_str = line[5:].strip()
                    if payload_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload_str)
                    except json.JSONDecodeError:
                        log.warning(
                            "cloud LLM emitted non-JSON SSE payload: %r",
                            payload_str[:120],
                        )
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    # Reasoning-field channel (DeepSeek-R1, Qwen reasoning).
                    # Some providers use `reasoning` instead of
                    # `reasoning_content`; accept both. Filter unconditionally
                    # — if it appears under thinking_style="none" that's a
                    # provider quirk, not something we want to leak.
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if reasoning:
                        if not in_reasoning_field:
                            in_reasoning_field = True
                            track_on_thinking(True)
                        continue
                    content = delta.get("content") or ""
                    if content:
                        if in_reasoning_field:
                            in_reasoning_field = False
                            track_on_thinking(False)
                        if gemma_style:
                            state["raw_capture"].append(content)
                        yield content
        except httpx.HTTPError as e:
            raise RuntimeError(f"cloud LLM request failed: {e}") from e
        finally:
            if in_reasoning_field:
                track_on_thinking(False)

    if gemma_style:
        # Cloud-served Gemma: reasoning is in-content (channel block), not
        # in a separate JSON field. Wrap the content stream through the
        # shared filter so the answer stays clean.
        out_stream = _filter_gemma_channel_blocks(
            _raw_content_deltas(), on_thinking=track_on_thinking
        )
    else:
        out_stream = _raw_content_deltas()

    for piece in out_stream:
        state["saw_any_content"] = True
        yield piece

    if not state["saw_any_content"]:
        log.warning("cloud LLM returned an empty stream (model=%s)", resolved.model_id)
    elif gemma_style and resolved.thinking and not state["saw_thinking"]:
        # Diagnostic mirrors the ollama path: Gemma chat template may not
        # honor the <|think|> system-prompt sentinel server-side (some cloud
        # hosts strip system-prompt-tokens or wrap the model with their own
        # chat template). Helps explain "why does cloud Gemma feel dumber".
        sample = "".join(state["raw_capture"])[:200]
        log.warning(
            "thinking enabled but cloud Gemma emitted no reasoning tags. Raw stream start: %r",
            sample,
        )


def chat(
    cfg: Config,
    messages: list[dict],
    *,
    temperature_override: float | None = None,
    max_tokens_override: int | None = None,
) -> str:
    return "".join(
        stream_chat(
            cfg,
            messages,
            temperature_override=temperature_override,
            max_tokens_override=max_tokens_override,
        )
    )


__all__ = ["stream_chat", "chat", "get_client"]
