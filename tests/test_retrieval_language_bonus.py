"""Unit tests for the language-bonus reranker bias added for #3.

The bonus is applied after the cross-encoder scores all candidates, so we can
exercise it in isolation by stubbing the Chroma query and the cross-encoder.
"""

from __future__ import annotations

import numpy as np

import student_bot.bot.retrieval as ret
from student_bot.config import get_config


class _FakeReranker:
    """Returns a fixed score per text. Indexed by the text snippet."""

    def __init__(self, score_by_text: dict[str, float]):
        self._score_by_text = score_by_text

    def predict(self, pairs):
        # pairs is list[(query, text)] — we score by text. Returns ndarray
        # to match the real CrossEncoder.predict() interface (callers do
        # `.tolist()` on the result).
        return np.array([self._score_by_text.get(text, 0.0) for _q, text in pairs])


def _stub_chroma_query(rows: list[dict]):
    """rows: each {id, text, rel_source, language, distance}. Returns the
    payload Chroma's `collection.query(...)` produces."""
    return {
        "ids": [[r["id"] for r in rows]],
        "documents": [[r["text"] for r in rows]],
        "metadatas": [
            [
                {
                    "rel_source": r["rel_source"],
                    "doc_title": r.get("doc_title", r["rel_source"]),
                    "doc_type": "md",
                    "language": r["language"],
                    "section_path": "",
                    "chunk_index": 0,
                    "page_start": 0,
                    "source_url": "",
                }
                for r in rows
            ]
        ],
        "distances": [[r.get("distance", 0.0) for r in rows]],
    }


def _patch(monkeypatch, rows: list[dict], score_by_text: dict[str, float]):
    class _Coll:
        def query(self, **_kwargs):
            return _stub_chroma_query(rows)

    monkeypatch.setattr(ret, "get_chroma_collection", lambda _cfg: _Coll())
    monkeypatch.setattr(ret, "encode_query", lambda _cfg, _q: np.zeros(1))
    monkeypatch.setattr(ret, "get_reranker", lambda _cfg: _FakeReranker(score_by_text))


def test_language_bonus_breaks_ties_in_favour_of_query_language(monkeypatch):
    """Two parallel SV/EN chunks that the cross-encoder scores identically:
    the SV chunk wins for a Swedish query, the EN chunk wins for an English one."""
    rows = [
        {"id": "sv1", "text": "T1", "rel_source": "kursval-sv.md", "language": "sv"},
        {"id": "en1", "text": "T2", "rel_source": "kursval-en.md", "language": "en"},
    ]
    cfg = get_config()
    _patch(monkeypatch, rows, {"T1": 1.0, "T2": 1.0})
    sv = ret.retrieve(cfg, "kursval", query_language="sv")
    assert sv.reranked[0].chunk_id == "sv1"

    _patch(monkeypatch, rows, {"T1": 1.0, "T2": 1.0})
    en = ret.retrieve(cfg, "course selection", query_language="en")
    assert en.reranked[0].chunk_id == "en1"


def test_language_bonus_does_not_override_clearly_better_other_language(monkeypatch):
    """If the cross-encoder scores the cross-language chunk much higher, the
    bonus is too small to flip ranking. Tunable via `reranker.language_bonus`."""
    rows = [
        {"id": "sv1", "text": "T1", "rel_source": "weak-sv.md", "language": "sv"},
        {"id": "en1", "text": "T2", "rel_source": "strong-en.md", "language": "en"},
    ]
    cfg = get_config()
    # Default bonus is 0.5; the gap between -0.3 and +2.0 (== 2.3) is well
    # beyond it, so the EN chunk still leads even on a Swedish query.
    _patch(monkeypatch, rows, {"T1": -0.3, "T2": 2.0})
    sv = ret.retrieve(cfg, "kursval", query_language="sv")
    assert sv.reranked[0].chunk_id == "en1"


def test_language_bonus_skipped_for_untagged_chunks(monkeypatch):
    """Chunks with empty `language` metadata get no bonus and no penalty —
    they sort by raw rerank score. (Old ingests / web-fetched chunks fall
    into this category.)"""
    rows = [
        {"id": "u1", "text": "T1", "rel_source": "untagged.md", "language": ""},
        {"id": "sv1", "text": "T2", "rel_source": "swedish.md", "language": "sv"},
    ]
    cfg = get_config()
    # Untagged chunk scores 0.6, Swedish scores 0.2. With bonus 0.5, Swedish
    # would climb to 0.7 and lead. (This documents the intended behaviour:
    # untagged chunks lose ties but not large-margin races to tagged chunks.)
    _patch(monkeypatch, rows, {"T1": 0.6, "T2": 0.2})
    sv = ret.retrieve(cfg, "kursval", query_language="sv")
    assert sv.reranked[0].chunk_id == "sv1"


def test_language_bonus_disabled_when_no_query_language(monkeypatch):
    """When the caller passes `query_language=None`, no bonus is applied
    regardless of the chunks' language tags — falls back to raw rerank
    sort, matching pre-#3 behaviour."""
    rows = [
        {"id": "sv1", "text": "T1", "rel_source": "sv.md", "language": "sv"},
        {"id": "en1", "text": "T2", "rel_source": "en.md", "language": "en"},
    ]
    cfg = get_config()
    _patch(monkeypatch, rows, {"T1": 0.3, "T2": 0.4})
    out = ret.retrieve(cfg, "kursval", query_language=None)
    # EN chunk wins on raw score; SV bonus is not applied.
    assert out.reranked[0].chunk_id == "en1"


def test_language_bonus_zero_config_disables_bias(monkeypatch):
    """Operators who want the old behaviour can set `language_bonus: 0` and
    queries pass through unchanged even with `query_language` set."""
    rows = [
        {"id": "sv1", "text": "T1", "rel_source": "sv.md", "language": "sv"},
        {"id": "en1", "text": "T2", "rel_source": "en.md", "language": "en"},
    ]
    cfg = get_config()
    cfg.reranker.language_bonus = 0.0
    _patch(monkeypatch, rows, {"T1": 0.3, "T2": 0.4})
    out = ret.retrieve(cfg, "kursval", query_language="sv")
    assert out.reranked[0].chunk_id == "en1"
