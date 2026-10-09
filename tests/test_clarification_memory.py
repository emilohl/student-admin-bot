"""A clarified question keeps its original ask, and costs one turn of memory.

On main, "Vilka spärrkurser finns…" → "CTFYS" → "HT2022" was routed as
"CTFYS HT2022". The second merge fused the reply with the nearest user message,
which by then was the first reply, so the question itself was gone. Each
clarification also held its own pair in memory, so turn 1 fell out of the
buffer after about three clarified questions.

The dynamic-web router is stubbed: it returns the clarifications in order and
records the query it was asked to route, which is what retrieval sees.
"""

from __future__ import annotations

import pytest

import student_bot.bot.pipeline as pipeline
from student_bot.bot import web_retrieval as wr
from student_bot.bot.gate import GateDecision
from student_bot.bot.memory import ConversationMemory
from student_bot.bot.pipeline import AnswerResult, answer, remember_turn
from student_bot.bot.retrieval import RetrievalResult
from student_bot.bot.web_retrieval import WebFetchResult
from student_bot.config import get_config

_QUESTION = "Vilka spärrkurser finns i årskurs 2?"
_PICK = WebFetchResult(
    clarification=(
        "Ditt program är inte entydigt. Vilket menar du? CTFYS eller TTFYM?",
        "Your program reference is ambiguous. CTFYS or TTFYM?",
    )
)
_ROUND = WebFetchResult(
    clarification=(
        "Vilken antagningsomgång gäller för dig? Ange t.ex. HT2022.",
        "Which admission round applies to you? E.g. HT2022.",
    )
)
# An early return that never reaches retrieval or the LLM, and is not stored.
_STOP = WebFetchResult(missing_kth_program=("Programmet finns inte.", "No such programme."))


@pytest.fixture
def cfg():
    return get_config()


@pytest.fixture
def router(monkeypatch):
    """Queue the router's answers; `router.queries` and `router.kwargs` hold
    what it was asked."""

    class _Router:
        def __init__(self):
            self.replies: list[WebFetchResult] = []
            self.queries: list[str] = []
            self.kwargs: list[dict] = []

        def __call__(self, _cfg, question, *_a, **kw):
            self.queries.append(question)
            self.kwargs.append(kw)
            return self.replies.pop(0)

    r = _Router()
    monkeypatch.setattr(pipeline, "maybe_fetch_dynamic_web", r)
    monkeypatch.setattr(pipeline, "_jargon", lambda _cfg: None)
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {"ctfys": "CTFYS"})
    return r


def _turn(cfg, memory: ConversationMemory, question: str) -> AnswerResult:
    """One turn the way the frontends run it: priors in, `remember_turn` out."""
    term_prior, year_prior = memory.get_admission_hints("u", "t")
    result = answer(
        question,
        history=memory.get("u", "t"),
        cfg=cfg,
        program_prior=memory.get_program_code("u", "t"),
        admission_term_prior=term_prior,
        admission_year_prefix_prior=year_prior,
        pending_question_prior=memory.get_pending_question("u", "t"),
    )
    remember_turn(memory, "u", "t", question, result)
    return result


def test_two_clarifications_in_a_row_keep_the_question(cfg, router):
    memory = ConversationMemory(cfg)
    router.replies = [_PICK, _ROUND, _STOP]

    _turn(cfg, memory, _QUESTION)
    _turn(cfg, memory, "CTFYS")
    _turn(cfg, memory, "HT2022")

    last = router.queries[-1]
    assert _QUESTION in last and "CTFYS" in last and "HT2022" in last


def test_a_clarified_question_holds_one_pair(cfg, router):
    memory = ConversationMemory(cfg)
    router.replies = [_PICK, _ROUND]

    _turn(cfg, memory, _QUESTION)
    _turn(cfg, memory, "CTFYS")

    turns = memory.get("u", "t")
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == f"{_QUESTION}\n\nCTFYS"
    assert memory.get_pending_question("u", "t") == f"{_QUESTION}\n\nCTFYS"


@pytest.mark.parametrize(
    "new_question",
    [
        "Vad är CSN?",
        "Hur mycket CSN fick man år 2023?",
        "När är omtentan 2025?",
        "När är omtentan VT2026?",
    ],
)
def test_a_new_question_instead_of_a_reply_is_not_merged(cfg, router, no_corpus_hit, new_question):
    memory = ConversationMemory(cfg)
    router.replies = [_ROUND, None]  # the new question is answered, so it is stored

    _turn(cfg, memory, _QUESTION)
    _turn(cfg, memory, new_question)

    assert router.queries[-1] == new_question
    assert memory.get_pending_question("u", "t") is None


def test_a_refused_input_keeps_the_pending_question(cfg, router):
    memory = ConversationMemory(cfg)
    router.replies = [_ROUND]

    _turn(cfg, memory, _QUESTION)
    result = _turn(cfg, memory, "x" * (cfg.guardrails.input_max_chars + 1))

    assert result.too_long
    assert memory.get_pending_question("u", "t") == _QUESTION


def test_a_failed_reply_keeps_the_question_for_the_retry(cfg, router):
    """A reply that is not stored (here an unknown programme code) leaves the
    clarification as the last turn, so the retry is still read as the reply."""
    memory = ConversationMemory(cfg)
    router.replies = [_PICK, _STOP, _ROUND]

    _turn(cfg, memory, _QUESTION)
    _turn(cfg, memory, "CTFYZ")
    _turn(cfg, memory, "CTFYS")

    assert router.queries[-1].startswith(_QUESTION)


# What the router returns when the programme has no admission in the given year.
_ADMISSION_NOT_FOUND = WebFetchResult(
    clarification=(
        "För att visa rätt utbildningsplan för **CTFYS** behöver jag veta vilken "
        "antagningsomgång som gäller.\n_(Din sökning matchade ingen period som börjar på 2019.)_",
        "To show the right study plan for **CTFYS** I need the admission round.",
    ),
    resolved_program_code="CTFYS",
    admission_not_found=True,
)


@pytest.mark.parametrize(
    "retry,admission",
    [
        ("HT2022", (None, "2022")),
        ("VT2022", (None, "2022")),
        ("hösten 2022", (None, "2022")),
        ("jag började 2022", (None, "2022")),
        ("2022", (None, "2022")),
        ("år 2022 var det", (None, "2022")),
        ("HT2022?", (None, "2022")),
        ("HT2022, vilka kurser blir det då?", (None, "2022")),
        ("ht22", (None, "2022")),
        ("höstterminen 2022", (None, "2022")),
        ("20222", ("20222", None)),
    ],
)
def test_a_year_with_no_plan_then_the_right_one_is_not_asked_again(cfg, router, retry, admission):
    """The newest reply's admission year is what the router uses, whatever form it is
    written in, and the rejected year is kept out of everything that is saved."""
    memory = ConversationMemory(cfg)
    router.replies = [_ROUND, _ADMISSION_NOT_FOUND, _STOP]

    _turn(cfg, memory, "Vilka kurser är obligatoriska i årskurs 2 på CTFYS?")
    _turn(cfg, memory, "HT2019")
    assert "2019" not in memory.get_pending_question("u", "t")
    assert all("2019" not in t["content"] for t in memory.get("u", "t") if t["role"] == "user")

    _turn(cfg, memory, retry)

    override = router.kwargs[-1]["admission_hints_override"]
    assert (override.exact_term, override.year_prefix) == admission
    assert "2019" not in router.queries[-1]
    assert "obligatoriska i årskurs 2" in router.queries[-1]


def test_a_whole_question_after_a_clarification_is_routed_on_its_own(cfg, router):
    """A question is never joined, even one that names its admission year. This
    one carries the programme and the year itself, and the rejected year stays out."""
    memory = ConversationMemory(cfg)
    router.replies = [_ROUND, _ADMISSION_NOT_FOUND, _STOP]

    _turn(cfg, memory, "Vilka kurser är obligatoriska i årskurs 2 på CTFYS?")
    _turn(cfg, memory, "HT2019")
    _turn(
        cfg, memory, "Vilka kurser är obligatoriska i årskurs 2 på CTFYS? Jag började hösten 2022."
    )

    query = router.queries[-1]
    assert query.count("Vilka kurser") == 1 and "hösten 2022" in query
    assert "2019" not in query
    assert router.kwargs[-1]["admission_hints_override"] is None


def test_a_programme_pick_with_a_missing_year_keeps_the_pick(cfg, router):
    """Only a reply to the admission year question is dropped. A pick that also names a
    year with no admission keeps the pick; the next reply's admission year decides."""
    memory = ConversationMemory(cfg)
    router.replies = [_PICK, _ADMISSION_NOT_FOUND, _STOP]

    _turn(cfg, memory, _QUESTION)
    _turn(cfg, memory, "CTFYS, jag började 2019")
    assert "CTFYS" in memory.get_pending_question("u", "t")

    _turn(cfg, memory, "HT2022")
    assert "CTFYS" in router.queries[-1]
    assert router.kwargs[-1]["admission_hints_override"].year_prefix == "2022"


def test_an_admission_year_that_exists_is_kept_when_another_clarification_follows(cfg, router):
    """Only an admission year the programme does not have is dropped. Here the admission year
    exists but the router asks for the five-digit code."""
    several = WebFetchResult(
        clarification=("Flera omgångar matchar. Vilken femsiffrig periodkod?", "Which code?"),
        resolved_program_code="CTFYS",
    )
    memory = ConversationMemory(cfg)
    router.replies = [_ROUND, several]

    _turn(cfg, memory, "Vilka kurser är obligatoriska i årskurs 2 på CTFYS?")
    _turn(cfg, memory, "HT2024")

    assert memory.get_pending_question("u", "t").endswith("HT2024")


def test_a_reply_without_an_admission_year_passes_no_override(cfg, router):
    memory = ConversationMemory(cfg)
    router.replies = [_PICK, _ROUND]

    _turn(cfg, memory, _QUESTION)
    _turn(cfg, memory, "CTFYS")

    assert router.kwargs[-1]["admission_hints_override"] is None


def _answered(**kw) -> AnswerResult:
    return AnswerResult(
        question=kw.pop("question", "q"),
        lang="sv",
        answered=True,
        answer="Svar",
        rendered="Svar",
        gate=GateDecision(True, "ok", 0.0, 0.0, 0),
        retrieval=RetrievalResult(query="q"),
        latency_ms=0,
        **kw,
    )


def test_an_answer_to_a_clarified_question_replaces_the_clarification(cfg):
    memory = ConversationMemory(cfg)
    memory.append("u", "t", "user", _QUESTION)
    memory.append("u", "t", "assistant", "Vilken antagningsomgång gäller för dig?")

    merged = f"{_QUESTION}\n\nHT2022"
    remember_turn(memory, "u", "t", "HT2022", _answered(merged_question=merged))

    assert memory.get("u", "t") == [
        {"role": "user", "content": merged},
        {"role": "assistant", "content": "Svar"},
    ]


def test_remember_turn_keeps_the_admission_year(cfg):
    """Mattermost used to drop it, and asked for the admission year again every turn."""
    memory = ConversationMemory(cfg)
    remember_turn(
        memory,
        "u",
        "t",
        "q",
        _answered(program_code="CTFYS", admission_term="20222", admission_year_prefix="2022"),
    )
    assert memory.get_program_code("u", "t") == "CTFYS"
    assert memory.get_admission_hints("u", "t") == ("20222", "2022")


# ---- which admission year is stored ------------------------------------
#
# Design decision: one admission year is in focus at a time, the one the
# conversation is currently about (not necessarily the student's own). An
# admission year that picked a programme's study plan replaces the stored one. An
# admission year in any other message changes nothing. See `routed_programme` in
# `pipeline.answer`.


@pytest.fixture
def no_corpus_hit(monkeypatch, router):
    """Nothing retrieved and the gate refuses: the meta-fallback path, which is
    where a bare "Förlåt, jag menar HT2024" lands. Records the messages the
    model would be sent."""
    sent: list[list[dict]] = []

    def _stream(_cfg, messages, on_thinking=None):
        sent.append(messages)
        yield "Svar"

    router.replies = [None] * 5
    monkeypatch.setattr(pipeline, "retrieve", lambda _cfg, q, **_kw: RetrievalResult(query=q))
    monkeypatch.setattr(
        pipeline, "evaluate_gate", lambda *_a, **_kw: GateDecision(False, "top1<-0.5", -5, -5, 0)
    )
    monkeypatch.setattr(pipeline, "_stream_answer", _stream)
    return sent


def test_a_course_only_fetch_does_not_change_the_admission_year(
    cfg, no_corpus_hit, router, monkeypatch
):
    """The router reports the parsed year on a course-page fetch too. Only a
    admission year that picked a programme's study plan may replace the stored one."""
    monkeypatch.setattr(
        pipeline, "evaluate_gate", lambda *_a, **_kw: GateDecision(True, "pass", 5, 5, 3)
    )
    router.replies = [WebFetchResult(applied_admission_year_prefix="2025")]
    memory = ConversationMemory(cfg)
    memory.set_program_code("u", "t", "CTFYS")
    memory.set_admission_hints("u", "t", exact_term="20242")

    _turn(cfg, memory, "När är omtentan i SF1625 VT2025?")

    assert memory.get_admission_hints("u", "t") == ("20242", None)


def test_a_routed_programme_admission_year_replaces_the_stored_one(
    cfg, no_corpus_hit, router, monkeypatch
):
    """A new year that picked the study plan replaces the old exact term; on
    main the old term stayed next to it and kept winning."""
    monkeypatch.setattr(
        pipeline, "evaluate_gate", lambda *_a, **_kw: GateDecision(True, "pass", 5, 5, 3)
    )
    router.replies = [
        WebFetchResult(resolved_program_code="CTFYS", applied_admission_year_prefix="2023")
    ]
    memory = ConversationMemory(cfg)
    memory.set_admission_hints("u", "t", exact_term="20242")

    _turn(cfg, memory, "Jag läser CTFYS, började HT2023. Vilka kurser har jag i årskurs 2?")

    assert memory.get_admission_hints("u", "t") == (None, "2023")


def test_a_correction_after_an_answer_naming_the_admission_year_is_not_merged(
    cfg, no_corpus_hit, router
):
    """An answer built on an admission year usually says "antagningsomgång", which the
    merge detectors also match. Merging fused "Förlåt, jag menar HT2024" with
    the old question, whose HT2025 then won the routing."""
    memory = ConversationMemory(cfg)
    memory.set_program_code("u", "t", "CTFYS")
    memory.set_admission_hints("u", "t", exact_term="20252")
    memory.append("u", "t", "user", "Jag läser CTFYS och började HT2025, vad gäller?")
    memory.append("u", "t", "assistant", "För antagningsomgång HT2025 gäller …")

    _turn(cfg, memory, "Förlåt, jag menar HT2024")

    assert router.queries[-1] == "Förlåt, jag menar HT2024"
    assert memory.get("u", "t")[1]["content"] == "För antagningsomgång HT2025 gäller …"


def test_a_bare_correction_does_not_change_the_admission_year(cfg, no_corpus_hit):
    """No programme in the message, so nothing was routed: the admission year stays until
    the student uses it for a programme again."""
    memory = ConversationMemory(cfg)
    memory.set_program_code("u", "t", "CTFYS")
    memory.set_admission_hints("u", "t", exact_term="20252")

    _turn(cfg, memory, "Förlåt, jag menar HT2024")

    assert memory.get_admission_hints("u", "t") == ("20252", None)


def test_a_turn_without_an_admission_year_keeps_the_stored_one(cfg, no_corpus_hit):
    memory = ConversationMemory(cfg)
    memory.set_admission_hints("u", "t", exact_term="20242")

    _turn(cfg, memory, "Hur anmäler jag mig till tentamen?")

    assert memory.get_admission_hints("u", "t") == ("20242", None)
    assert "antagningsomgång HT2024" in no_corpus_hit[-1][-1]["content"]


def test_the_facts_come_from_earlier_turns_only(cfg, no_corpus_hit, router):
    """The current message speaks for itself; a one-shot question gets no facts line."""
    router.replies = [
        WebFetchResult(resolved_program_code="CTFYS", applied_admission_year_prefix="2024"),
        None,
    ]
    memory = ConversationMemory(cfg)
    _turn(cfg, memory, "Jag började HT2024 på CTFYS, vad gäller?")
    assert not no_corpus_hit[-1][-1]["content"].startswith("Från samtalet")

    memory.set_program_code("u", "t", "CTFYS")
    memory.set_admission_hints("u", "t", exact_term="20242")
    _turn(cfg, memory, "Hur anmäler jag mig till tentamen?")
    assert no_corpus_hit[-1][-1]["content"].startswith("Från samtalet hittills: program CTFYS")


def test_a_merge_after_an_ordinary_answer_keeps_that_answer(cfg, no_corpus_hit):
    """The merge detectors also match an answer that mentions "antagningsomgång".
    Only a turn after a real clarification (a pending question) is folded."""
    memory = ConversationMemory(cfg)
    memory.append("u", "t", "user", "Hur vet jag vilken utbildningsplan som gäller?")
    memory.append("u", "t", "assistant", "Det beror på din antagningsomgång, se kth.se.")

    _turn(cfg, memory, "Jag började HT2023, vilka kurser är obligatoriska i årskurs 2?")

    turns = memory.get("u", "t")
    assert len(turns) == 4
    assert turns[1]["content"] == "Det beror på din antagningsomgång, se kth.se."
