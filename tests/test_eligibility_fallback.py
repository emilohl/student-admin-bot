"""Regression tests for the cross-cohort eligibility-block fallback.

Background: KTH's per-programme study-plan pages sometimes carry a
"behörighetsgivande kurser per masterprogram" block that maps each target
master to the courses a student must complete to qualify. The block is
critical for "what year-3 electives qualify me for the X master?" questions,
but its presence varies across cohorts (CTFYS HT2023 has it, HT2024 does not)
and across programmes (CFATE has it, CELTE does not).

These tests pin the three behaviours we rely on:

- Extractor finds the block in CTFYS HT2023 and CFATE HT2023, returns None
  for CTFYS HT2024 (block was dropped) and CELTE HT2023 (programme never
  carried it).
- When the requested term carries the block, _studyplan_chunks_from_html
  emits it as a dedicated, citable chunk.
- When the requested term lacks the block, _maybe_emit_fallback_eligibility
  fetches a prior term and surfaces a labeled, caveated chunk pointing back
  at the prior cohort. No fallback fires when the current term already has
  one — and no fallback chunk is added when no prior term has one either.

Fixtures are real KTH HTML captured 2026-05-10 under tests/fixtures/. Refresh
by re-fetching the same URLs if KTH's structure ever changes.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import student_bot.bot.web_retrieval as wr
from student_bot.config import get_config


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# Map of URL -> fixture filename. Used by the stubbed _fetch_html so the
# fallback path can resolve "prior-term" fetches without hitting the network.
_URL_FIXTURES = {
    "https://www.kth.se/student/kurser/program/CTFYS/20232/arskurs3": "program_CTFYS_20232_arskurs3.html",
    "https://www.kth.se/student/kurser/program/CTFYS/20242/arskurs3": "program_CTFYS_20242_arskurs3.html",
    "https://www.kth.se/student/kurser/program/CFATE/20232/arskurs3": "program_CFATE_20232_arskurs3.html",
    "https://www.kth.se/student/kurser/program/CELTE/20232/arskurs3": "program_CELTE_20232_arskurs3.html",
}


@pytest.fixture
def cfg():
    return get_config()


@pytest.fixture
def patterns(cfg):
    return wr._compiled_patterns(cfg)


@pytest.fixture
def stub_network(monkeypatch):
    """Replace network primitives with fixture-backed equivalents.

    `_fetch_html` resolves URLs from `_URL_FIXTURES`; unknown URLs raise so a
    test never silently passes by hitting the real network. `_is_allowed_url`
    is bypassed because the fixture URLs are stable and we don't want allowlist
    drift to bleed into eligibility-fallback tests.
    """

    def _stub_fetch(url, _cfg):
        name = _URL_FIXTURES.get(url)
        if not name:
            raise FileNotFoundError(f"no fixture registered for {url}")
        return url, _load(name)

    monkeypatch.setattr(wr, "_fetch_html", _stub_fetch)
    monkeypatch.setattr(wr, "_is_allowed_url", lambda u, c, p: True)


class _FakeCache:
    """No-op stand-in for WebCache — the eligibility chunk path only ever
    `put`s, never reads. Tests don't assert on cache state."""

    def put(self, *_a, **_k):
        return None

    def get(self, *_a, **_k):
        return None


# -----------------------------------------------------------------------------
# Extractor: _eligibility_text_from_store
# -----------------------------------------------------------------------------


def _store_for(name: str):
    return wr._compressed_application_store(_load(name))


def test_extractor_finds_block_in_ctfys_ht2023():
    text = wr._eligibility_text_from_store(_store_for("program_CTFYS_20232_arskurs3.html"))
    assert text is not None
    # The Datalogi entry (with KTH's own typo) must appear, with all three
    # required courses, so retrieval has the answer the bot needs.
    assert "Datatologi" in text or "datalogi" in text.lower()
    assert "DD2352" in text
    assert "SF1679" in text
    assert "DD1380" in text


def test_extractor_finds_block_in_cfate_ht2023():
    text = wr._eligibility_text_from_store(_store_for("program_CFATE_20232_arskurs3.html"))
    assert text is not None
    # CFATE uses a different format ("Behörighetsgivande kurser till
    # masterprogram" header + master codes in parens). TTFYM (Teknisk fysik)
    # eligibility must surface — that's the user's example.
    assert "TTFYM" in text
    assert "SI1146" in text
    assert "SH1014" in text


def test_extractor_returns_none_for_ctfys_ht2024():
    # KTH dropped the block from CTFYS HT2024. Without this returning None,
    # the cross-cohort fallback would never fire.
    assert wr._eligibility_text_from_store(_store_for("program_CTFYS_20242_arskurs3.html")) is None


def test_extractor_returns_none_for_celte_ht2023():
    # CELTE never carries the block at all. The fallback can't help here;
    # this test pins that the extractor doesn't false-positive on adjacent
    # text like "behörighet" without "behörighetsgivande".
    assert wr._eligibility_text_from_store(_store_for("program_CELTE_20232_arskurs3.html")) is None


# -----------------------------------------------------------------------------
# Term-label helper
# -----------------------------------------------------------------------------


def test_term_label_sv_decodes_kth_term_codes():
    assert wr._term_label_sv("20232") == "HT2023"
    assert wr._term_label_sv("20241") == "VT2024"
    # Junk passes through unchanged so callers don't have to defend against
    # malformed inputs from upstream JSON.
    assert wr._term_label_sv("not-a-term") == "not-a-term"


# -----------------------------------------------------------------------------
# Current-term emission via _studyplan_chunks_from_html
# -----------------------------------------------------------------------------


def _eligibility_chunks(chunks):
    return [c for c in chunks if "behörighetsgivande" in (c.section_path or "").lower()]


def test_current_term_emits_eligibility_chunk_when_block_present():
    html = _load("program_CTFYS_20232_arskurs3.html")
    url = "https://www.kth.se/student/kurser/program/CTFYS/20232/arskurs3"
    chunks = wr._studyplan_chunks_from_html(html, final_url=url, fetched_at=int(time.time()))
    elig = _eligibility_chunks(chunks)
    assert len(elig) == 1, "exactly one eligibility chunk should be emitted"
    c = elig[0]
    assert "behorighet-master" in (c.chunk_id or ""), "chunk-id fragment is the dedup key"
    assert c.section_path.startswith("Årskurs 3"), c.section_path
    assert "DD2352" in c.text
    assert "SF1679" in c.text
    assert "DD1380" in c.text


def test_current_term_emits_no_eligibility_chunk_when_block_missing():
    html = _load("program_CTFYS_20242_arskurs3.html")
    url = "https://www.kth.se/student/kurser/program/CTFYS/20242/arskurs3"
    chunks = wr._studyplan_chunks_from_html(html, final_url=url, fetched_at=int(time.time()))
    assert _eligibility_chunks(chunks) == []


# -----------------------------------------------------------------------------
# Cross-cohort fallback: _maybe_emit_fallback_eligibility
# -----------------------------------------------------------------------------


def test_fallback_fills_missing_block_from_prior_cohort(stub_network, cfg, patterns):
    """Requested term is HT2024 (no block); prior HT2023 has it. The fallback
    must emit one chunk, point back at HT2023, and carry the OBS! caveat so
    the LLM (and the user) can see this isn't current-cohort content."""
    html = _load("program_CTFYS_20242_arskurs3.html")
    url = "https://www.kth.se/student/kurser/program/CTFYS/20242/arskurs3"
    structured = wr._studyplan_chunks_from_html(html, final_url=url, fetched_at=int(time.time()))
    assert _eligibility_chunks(structured) == []

    chunks: list = list(structured)
    source_urls: list = []
    visited: set = {url}

    wr._maybe_emit_fallback_eligibility(
        html=html,
        final_url=url,
        structured=structured,
        cfg=cfg,
        patterns=patterns,
        cache=_FakeCache(),
        visited=visited,
        chunks=chunks,
        source_urls=source_urls,
    )

    fallback = [c for c in chunks if "fallback" in (c.chunk_id or "")]
    assert len(fallback) == 1
    c = fallback[0]
    # Citation hygiene: the section_path and doc_title must name the prior
    # cohort so the user knows which läsår the answer was sourced from.
    assert "HT2023" in c.section_path
    assert "HT2023" in c.doc_title
    # Caveat must be in the chunk text — it's what the LLM reproduces.
    assert "OBS!" in c.text
    assert "HT2023" in c.text and "HT2024" in c.text
    assert "studievägledaren" in c.text.lower()
    # The user-facing source URL points at the *prior* cohort so click-through
    # lands on the actual page that has the block.
    assert c.source_url == "https://www.kth.se/student/kurser/program/CTFYS/20232/arskurs3"
    # And the eligibility data itself made it through extraction.
    assert "DD2352" in c.text
    assert "SF1679" in c.text
    assert "DD1380" in c.text


def test_fallback_noop_when_current_term_already_has_block(stub_network, cfg, patterns):
    """HT2023 carries the block natively — the fallback path must not stack
    a redundant prior-term chunk on top."""
    html = _load("program_CTFYS_20232_arskurs3.html")
    url = "https://www.kth.se/student/kurser/program/CTFYS/20232/arskurs3"
    structured = wr._studyplan_chunks_from_html(html, final_url=url, fetched_at=int(time.time()))
    assert len(_eligibility_chunks(structured)) == 1

    chunks: list = list(structured)
    source_urls: list = []
    visited: set = {url}

    wr._maybe_emit_fallback_eligibility(
        html=html,
        final_url=url,
        structured=structured,
        cfg=cfg,
        patterns=patterns,
        cache=_FakeCache(),
        visited=visited,
        chunks=chunks,
        source_urls=source_urls,
    )

    fallback = [c for c in chunks if "fallback" in (c.chunk_id or "")]
    assert fallback == []


def test_fallback_noop_when_no_prior_term_has_block(stub_network, cfg, patterns, monkeypatch):
    """CELTE never carries the block. The fallback must give up cleanly
    rather than, say, raising or attaching the wrong programme's data."""

    # Drop the prior-term URLs to a single CELTE candidate so the helper
    # only has the one (also-empty) page to try. This isolates the
    # "tried, none had block" path from the cap-on-attempts logic.
    def _only_one_celte_prior(_code, _term, _year, _store, *, limit=3):
        return ["https://www.kth.se/student/kurser/program/CELTE/20232/arskurs3"]

    monkeypatch.setattr(wr, "_prior_term_year_urls", _only_one_celte_prior)

    html = _load("program_CELTE_20232_arskurs3.html")
    # Pretend we requested a non-existent CELTE/20242 URL so the regex
    # accepts it. Helper extracts programme code and year from the URL,
    # not from the HTML.
    url = "https://www.kth.se/student/kurser/program/CELTE/20242/arskurs3"
    structured = wr._studyplan_chunks_from_html(html, final_url=url, fetched_at=int(time.time()))

    chunks: list = list(structured)
    source_urls: list = []
    visited: set = {url}

    wr._maybe_emit_fallback_eligibility(
        html=html,
        final_url=url,
        structured=structured,
        cfg=cfg,
        patterns=patterns,
        cache=_FakeCache(),
        visited=visited,
        chunks=chunks,
        source_urls=source_urls,
    )

    # No fallback chunk added; original structured chunk count unchanged.
    assert len(chunks) == len(structured)
    assert all("fallback" not in (c.chunk_id or "") for c in chunks)
