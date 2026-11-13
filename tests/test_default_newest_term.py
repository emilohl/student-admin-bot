"""A programme question with no admission round uses the newest one instead of
asking (`dynamic_web.default_newest_term`).

The defaulted term is not something the student said, so it never reaches
memory: `applied_admission_term` stays empty and the term travels separately as
`defaulted_admission_term`, which the rendered answer names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import student_bot.bot.pipeline as pipeline
import student_bot.bot.web_retrieval as wr
from student_bot.bot.memory import ConversationMemory
from student_bot.bot.pipeline import answer, remember_turn, turn_priors
from student_bot.bot.retrieval import RetrievalResult, RetrievedChunk
from student_bot.bot.web_retrieval import AdmissionHints, WebFetchResult, _select_programme_urls
from student_bot.config import get_config

_ROOT = "https://www.kth.se/student/kurser/program/CTFYS"
_TERMS = ["20252", "20242", "20241", "20232"]
_PAGE = (Path(__file__).parent / "fixtures" / "program_CTFYS_20242_arskurs3.html").read_text(
    encoding="utf-8"
)


# --- the router ----------------------------------------------------------


def test_no_hint_picks_the_newest_term():
    r = _select_programme_urls("CTFYS", _TERMS, AdmissionHints(), default_newest=True)
    assert r.queue_urls == [f"{_ROOT}/20252"]
    assert r.defaulted_term == "20252" and not r.clarification_sv


def test_a_year_with_several_terms_picks_the_newest_of_them():
    r = _select_programme_urls(
        "CTFYS", _TERMS, AdmissionHints(year_prefix="2024"), default_newest=True
    )
    assert r.queue_urls == [f"{_ROOT}/20242"]
    assert r.defaulted_term == "20242" and not r.clarification_sv


@pytest.mark.parametrize(
    "hints", [AdmissionHints(exact_term="20241"), AdmissionHints(year_prefix="2023")]
)
def test_a_term_the_student_pinned_down_is_not_defaulted(hints):
    r = _select_programme_urls("CTFYS", _TERMS, hints, default_newest=True)
    assert r.queue_urls and r.defaulted_term is None


def test_a_year_with_no_admission_still_asks():
    r = _select_programme_urls(
        "CTFYS", _TERMS, AdmissionHints(year_prefix="2030"), default_newest=True
    )
    assert not r.queue_urls and r.admission_not_found and r.defaulted_term is None


def test_a_year_independent_question_carries_no_default():
    """It already used the newest term; the answer does not vary by cohort."""
    r = _select_programme_urls(
        "CTFYS", _TERMS, AdmissionHints(), question="Vem är PA för CTFYS?", default_newest=True
    )
    assert r.queue_urls == [f"{_ROOT}/20252"] and r.defaulted_term is None


# --- through maybe_fetch_dynamic_web, with the flag from config -----------


class _NoCache:
    def put(self, *_a, **_k):
        return None

    def get(self, *_a, **_k):
        return None


@pytest.fixture
def kth(monkeypatch):
    """Every KTH programme page is the CTFYS fixture; the root lists `_TERMS`."""
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {"ctfys": "CTFYS"})
    monkeypatch.setattr(wr, "_fetch_html", lambda url, _cfg, **_kw: (url, _PAGE))
    monkeypatch.setattr(wr, "_normalized_programme_terms_from_store", lambda _s: list(_TERMS))
    monkeypatch.setattr(wr, "WebCache", lambda _cfg: _NoCache())


def _cfg(default_newest_term: bool):
    cfg = get_config().model_copy(deep=True)
    cfg.dynamic_web.enabled = True
    cfg.dynamic_web.default_newest_term = default_newest_term
    return cfg


def test_the_newest_term_is_used_but_not_reported_as_applied(kth):
    r = wr.maybe_fetch_dynamic_web(_cfg(True), "Vad gäller i årskurs 3 på CTFYS?", "sv")
    assert r is not None and r.chunks and r.clarification is None
    assert any("/CTFYS/20252/arskurs3" in u for u in r.source_urls)
    assert r.defaulted_admission_term == "20252"
    assert r.applied_admission_term is None and r.applied_admission_year_prefix is None


def test_a_stated_year_is_still_reported_as_applied(kth):
    q = "Vad gäller i årskurs 3 på CTFYS? Jag började 2024"
    r = wr.maybe_fetch_dynamic_web(_cfg(True), q, "sv")
    assert r is not None and r.clarification is None
    assert any("/CTFYS/20242/arskurs3" in u for u in r.source_urls)
    assert r.defaulted_admission_term == "20242"
    assert r.applied_admission_term is None and r.applied_admission_year_prefix == "2024"


@pytest.mark.parametrize(
    "q,asked",
    [
        ("Vad gäller i årskurs 3 på CTFYS?", "antagningsomgång"),
        ("Vad gäller i årskurs 3 på CTFYS? Jag började 2024", "femsiffrig periodkod"),
    ],
)
def test_with_the_flag_off_it_asks_as_before(kth, q, asked):
    r = wr.maybe_fetch_dynamic_web(_cfg(False), q, "sv")
    assert r is not None and r.clarification and asked in r.clarification[0]
    assert r.defaulted_admission_term is None


# --- the rendered answer ---------------------------------------------------


@pytest.fixture
def web_answer(monkeypatch):
    """A study-plan answer from the dynamic-web path; returns a setter for the
    router's `defaulted_admission_term`."""
    url = f"{_ROOT}/20242/arskurs3"
    chunk = RetrievedChunk(
        chunk_id=f"web:{url}",
        text="Obligatoriska kurser i årskurs 3.",
        rel_source=url,
        doc_title="CTFYS årskurs 3",
        doc_type="html",
        language="sv",
        section_path="",
        chunk_index=0,
        chroma_distance=0.0,
        rerank_score=3.5,
        source_url=url,
    )
    defaulted: dict[str, str | None] = {"term": None}

    def _router(*_a, **_kw):
        return WebFetchResult(
            chunks=[chunk],
            source_urls=[url],
            resolved_program_code="CTFYS",
            defaulted_admission_term=defaulted["term"],
        )

    monkeypatch.setattr(pipeline, "maybe_fetch_dynamic_web", _router)
    monkeypatch.setattr(pipeline, "_jargon", lambda _cfg: None)
    monkeypatch.setattr(pipeline, "_rerank_web_chunks", lambda _cfg, _q, _l, cands: list(cands))
    monkeypatch.setattr(pipeline, "retrieve", lambda _cfg, q, **_kw: RetrievalResult(query=q))
    monkeypatch.setattr(pipeline, "_stream_answer", lambda *_a, **_kw: iter(["Svar."]))
    monkeypatch.setattr(pipeline, "literacy_footer", lambda *_a, **_kw: "_Tips._")
    return defaulted


_SV = "Svaret utgår från utbildningsplanen för antagna HT2024."
_EN = "This answer is based on the study plan for students admitted autumn 2024."
_SV_AFTER = "Antogs du ett annat år, välj det ovanför frågan."
_EN_AFTER = "If you were admitted another year, pick it above the question."


@pytest.mark.parametrize(
    "q,line,after",
    [
        ("Vilka kurser är obligatoriska i årskurs 3 på CTFYS?", _SV, _SV_AFTER),
        ("Which courses are mandatory in year 3 of CTFYS?", _EN, _EN_AFTER),
    ],
)
def test_a_defaulted_term_is_named_in_the_answer(web_answer, q, line, after):
    web_answer["term"] = "20242"
    streamed: list[str] = []
    memory = ConversationMemory(get_config())

    result = answer(
        q,
        cfg=get_config(),
        on_token=streamed.append,
        channel="web",
        **turn_priors(memory, "u", "t"),
    )
    remember_turn(memory, "u", "t", q, result)

    assert line in "".join(streamed)
    # Right before the literacy footer.
    assert result.rendered.split("\n\n")[-2:] == [line + " " + after, "_Tips._"]
    assert result.defaulted_admission_term == "20242"
    assert memory.get_admission_hints("u", "t") == (None, None)


@pytest.mark.parametrize(
    "q,after",
    [
        ("Vilka kurser är obligatoriska i årskurs 3 på CTFYS?", "skriv det i frågan."),
        ("Which courses are mandatory in year 3 of CTFYS?", "say so in your question."),
    ],
)
def test_without_the_picker_the_line_asks_for_the_year_in_the_question(web_answer, q, after):
    """Mattermost and the CLI have no picker above the question to point at."""
    web_answer["term"] = "20242"
    result = answer(q, cfg=get_config(), channel="mattermost")
    assert result.rendered.split("\n\n")[-2].endswith(after)


def test_no_line_without_a_defaulted_term(web_answer):
    result = answer("Vilka kurser är obligatoriska i årskurs 3 på CTFYS?", cfg=get_config())
    assert "utbildningsplanen för antagna" not in result.rendered
    assert result.defaulted_admission_term is None


def test_the_default_is_the_newest_started_cohort(monkeypatch):
    """KTH lists the next two autumns ahead of time; nobody is admitted yet."""
    monkeypatch.setattr(wr, "latest_cohort", lambda _now: 2026)
    future = ["20272", "20262", "20252", "20242"]
    r = _select_programme_urls("CTFYS", future, AdmissionHints(), default_newest=True)
    assert r.defaulted_term == "20262"
    r = _select_programme_urls("CTFYS", ["20282", "20272"], AdmissionHints(), default_newest=True)
    assert r.defaulted_term == "20272"  # only future rounds: the first of them
    r = _select_programme_urls("CTFYS", future, AdmissionHints(), question="Vem är PA för CTFYS?")
    assert r.queue_urls == [f"{_ROOT}/20262"] and r.defaulted_term is None
