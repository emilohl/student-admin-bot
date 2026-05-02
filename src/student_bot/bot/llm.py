"""Ollama client wrapper. Streaming chat completion."""
from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from ollama import Client

from student_bot.config import Config


@lru_cache(maxsize=1)
def _client(url: str) -> Client:
    return Client(host=url)


def get_client(cfg: Config) -> Client:
    return _client(cfg.llm.ollama_url)


def stream_chat(cfg: Config, messages: list[dict]) -> Iterator[str]:
    """Yield response text deltas. Caller joins."""
    client = get_client(cfg)
    options = {
        "num_ctx": cfg.llm.num_ctx,
        "temperature": cfg.llm.temperature,
        "num_predict": cfg.llm.max_tokens,
    }
    for chunk in client.chat(model=cfg.llm.model, messages=messages, stream=True, options=options):
        delta = chunk.get("message", {}).get("content", "")
        if delta:
            yield delta


def chat(cfg: Config, messages: list[dict]) -> str:
    return "".join(stream_chat(cfg, messages))


__all__ = ["stream_chat", "chat", "get_client"]
