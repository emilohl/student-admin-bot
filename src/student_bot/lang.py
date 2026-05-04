"""Language detection.

Restricted to Swedish + English to match the corpus and product scope.
Falls back to Swedish on ambiguity (most queries from KTH students).
"""

from __future__ import annotations

from functools import lru_cache

from lingua import Language, LanguageDetector, LanguageDetectorBuilder


@lru_cache(maxsize=1)
def _detector() -> LanguageDetector:
    return (
        LanguageDetectorBuilder.from_languages(Language.SWEDISH, Language.ENGLISH)
        .with_minimum_relative_distance(0.10)
        .build()
    )


def detect(text: str) -> str:
    """Return ISO code 'sv' or 'en'. Defaults to 'sv' on uncertainty."""
    if not text or not text.strip():
        return "sv"
    lang = _detector().detect_language_of(text)
    if lang == Language.ENGLISH:
        return "en"
    return "sv"


__all__ = ["detect"]
