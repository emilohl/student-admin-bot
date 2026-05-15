"""Lightweight zero-shot topic classifier.

Runs AFTER the answer is produced so it never adds user-visible latency.
Uses the currently-active LLM (whichever model `cfg.llm.active` selects)
with a lower temperature and tight token budget; the call goes through
the shared `stream_chat` dispatch so it works for both local Ollama and
cloud providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from student_bot.bot.llm import chat
from student_bot.config import Config


@dataclass
class Topic:
    id: str
    sv: str
    en: str
    hint: str

    def label(self, lang: str) -> str:
        return self.en if lang == "en" else self.sv


@lru_cache(maxsize=1)
def _load_topics(file_path: str) -> list[Topic]:
    raw = yaml.safe_load(Path(file_path).read_text(encoding="utf-8")) or []
    return [Topic(**t) for t in raw]


def load_topics(cfg: Config) -> list[Topic]:
    path = cfg.absolute(Path(cfg.topics.file))
    return _load_topics(str(path))


def _classification_prompt(topics: list[Topic], question: str, lang: str) -> str:
    if lang == "en":
        header = (
            "You are a classifier. Choose the single topic id that best matches "
            "the user's question, from the list below. Reply with the id only."
        )
    else:
        header = (
            "Du är en klassificerare. Välj det enda ämnes-id som bäst matchar "
            "användarens fråga, från listan nedan. Svara endast med id:t."
        )
    lines = [header, "", "Topics:"]
    for t in topics:
        label = t.label(lang)
        lines.append(f"- {t.id}: {label} — {t.hint}")
    lines.append("")
    lines.append(f"Question: {question}")
    lines.append("Topic id:")
    return "\n".join(lines)


def classify(cfg: Config, question: str, lang: str) -> tuple[str, float]:
    """Return (topic_id, confidence). Confidence is a heuristic: 1.0 if the
    model returned exactly an id from the list, else 0.5 if a substring match
    succeeded, else 0.0 with topic_id='other'."""
    if not cfg.topics.enabled:
        return ("other", 0.0)
    topics = load_topics(cfg)
    valid_ids = {t.id for t in topics}
    if not topics:
        return ("other", 0.0)

    prompt = _classification_prompt(topics, question, lang)
    try:
        raw = (
            chat(
                cfg,
                [{"role": "user", "content": prompt}],
                temperature_override=cfg.topics.classifier_temperature,
                max_tokens_override=16,
            )
            .strip()
            .lower()
        )
    except Exception:
        return ("other", 0.0)
    # The model often emits prose around the id; take the first token-ish word.
    first = raw.splitlines()[0].split()[0].strip(".,:'\"`*") if raw else ""

    if first in valid_ids:
        return (first, 1.0)
    # Substring fallback — covers "topic id: examination_grading".
    for tid in valid_ids:
        if tid in raw:
            return (tid, 0.5)
    return ("other", 0.0)


__all__ = ["Topic", "load_topics", "classify"]
