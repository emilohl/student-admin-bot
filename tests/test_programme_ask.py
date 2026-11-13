"""A course question with no code, no programme known and nothing in the
corpus asks for the programme instead of answering that nothing was found.
The web picker answers it with one click (meta.pending_question); a typed
code is merged with the waiting question like the other programme picks."""

import pytest

import student_bot.bot.course_resolver as course_resolver
import student_bot.bot.pipeline as pipeline
from student_bot.bot.gate import GateDecision
from student_bot.bot.pipeline import answer
from student_bot.bot.retrieval import RetrievalResult
from student_bot.bot.web_retrieval import merge_programme_clarification_followup
from student_bot.config import get_config

Q = "Jag klarade inte tentan i differentialekvationer, kan jag fortfarande läsa reglerteknik?"
ASK_SV = "Vilket program läser du?"


NAMES = frozenset({"Reglerteknik, allmän kurs", "Differentialekvationer och transformmetoder"})


@pytest.fixture
def empty_corpus(monkeypatch):
    """No web route, no corpus hit, an LLM that would answer 'Svar.', and two
    course names in the study-plan cache."""
    monkeypatch.setattr(course_resolver, "cached_course_names", lambda _cfg: NAMES)
    monkeypatch.setattr(pipeline, "maybe_fetch_dynamic_web", lambda *_a, **_kw: None)
    monkeypatch.setattr(pipeline, "_jargon", lambda _cfg: None)
    monkeypatch.setattr(pipeline, "retrieve", lambda _cfg, q, **_kw: RetrievalResult(query=q))
    monkeypatch.setattr(
        pipeline, "evaluate_gate", lambda _cfg, _r: GateDecision(False, "top1<-0.5", -4.0, -4.0, 0)
    )
    monkeypatch.setattr(pipeline, "_stream_answer", lambda *_a, **_kw: iter(["Svar."]))
    monkeypatch.setattr(pipeline, "literacy_footer", lambda *_a, **_kw: "_Tips._")


@pytest.mark.parametrize(
    "channel,how",
    [
        ("web", "Välj det ovanför frågan eller skriv programkoden"),
        ("mattermost", "Skriv programkoden"),
    ],
)
def test_asks_for_the_programme_when_the_corpus_has_nothing(empty_corpus, channel, how):
    result = answer(Q, cfg=get_config(), channel=channel)
    assert result.gate.reason == "programme_clarification"
    assert not result.answered
    assert result.answer.startswith(ASK_SV) and how in result.answer
    assert result.rendered == result.answer


def test_the_question_in_english_is_asked_in_english(empty_corpus):
    result = answer(
        "I failed my exam, can I still take the course Reglerteknik this year as planned?",
        cfg=get_config(),
    )
    assert result.answer.startswith("Which programme do you study?")


def test_with_a_programme_known_it_falls_back_as_before(empty_corpus):
    result = answer(Q, cfg=get_config(), program_prior="CTFYS")
    assert result.gate.reason != "programme_clarification"
    assert "Svar." in result.answer


@pytest.mark.parametrize(
    "q",
    [
        "Hur överklagar jag ett beslut om tillgodoräknande?",  # no course named
        "Hur anmäler jag mig till tentamen?",  # course intent, but no course named
        "Vilka förkunskaper kräver SF1683?",  # a code: the course page is fetched instead
    ],
)
def test_no_ask_without_a_course_named(empty_corpus, q):
    assert answer(q, cfg=get_config()).gate.reason != "programme_clarification"


def test_a_typed_code_is_merged_with_the_waiting_question():
    history = [
        {"role": "user", "content": Q},
        {"role": "assistant", "content": f"{ASK_SV} Skriv programkoden, t.ex. CTFYS."},
    ]
    merged = merge_programme_clarification_followup(
        "CTFYS", history, get_config(), pending_question=Q
    )
    assert merged == f"{Q}\n\nCTFYS"
    assert (
        merge_programme_clarification_followup("tack", history, get_config(), pending_question=Q)
        == "tack"
    )


def test_course_names_come_from_the_cached_study_plans(tmp_path, monkeypatch):
    from student_bot.bot.web_cache import WebCache

    cfg = get_config().model_copy(deep=True)
    cfg.dynamic_web.cache_db = str(tmp_path / "web_cache.sqlite")
    cache = WebCache(cfg)
    rows = [
        (
            "https://www.kth.se/student/kurser/program/CTFYS/20242/arskurs3#study-plan",
            '[{"code": "EL1000", "name": "Reglerteknik, allmän kurs"}]',
        ),
        ("https://www.kth.se/student/kurser/kurs/EL1000", "not a study plan"),
    ]
    with cache._connect() as conn:
        for url, content in rows:
            conn.execute(
                "INSERT INTO web_cache (url, title, content, fetched_at) VALUES (?, '', ?, 0)",
                (url, content),
            )
    monkeypatch.setattr(course_resolver, "_cached_names", None)
    assert course_resolver.cached_course_names(cfg) == {"Reglerteknik, allmän kurs"}
    assert course_resolver.question_names_a_course(cfg, "Vad kräver reglerteknik?")
    assert not course_resolver.question_names_a_course(cfg, "Hur anmäler jag mig till tentamen?")
