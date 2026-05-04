"""Lightweight zero-shot topic classifier.

Runs AFTER the answer is produced so it never adds user-visible latency.
Uses the same Gemma model that's already loaded in Ollama.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from student_bot.bot.llm import get_client
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
    client = get_client(cfg)
    try:
        resp = client.chat(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": prompt}],
            options={
                "num_ctx": cfg.llm.num_ctx,
                "temperature": cfg.topics.classifier_temperature,
                "num_predict": 16,
            },
        )
    except Exception:
        return ("other", 0.0)

    raw = (resp.get("message", {}).get("content") or "").strip().lower()
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
