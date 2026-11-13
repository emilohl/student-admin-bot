"""The course-without-code path: a course named in the question is found
without cutting a phrase out of the sentence (the picker makes the programme
known even when the question carries no code)."""

import pytest

from student_bot.bot import course_resolver as cr
from student_bot.bot import web_retrieval
from student_bot.config import get_config

CTFYS_COURSES = [
    ("DD1331", "Grundläggande programmering"),
    ("SF1672", "Linjär algebra"),
    ("SF1673", "Analys i en variabel"),
    ("SF1674", "Flervariabelanalys"),
    ("SF1683", "Differentialekvationer och transformmetoder"),
    ("EL1000", "Reglerteknik, allmän kurs"),
]


@pytest.fixture
def ctfys(monkeypatch):
    monkeypatch.setattr(web_retrieval, "_cached_terms_for_code", lambda cfg, code: ["20252"])
    monkeypatch.setattr(cr, "_fetch_program_courses", lambda cfg, code, term: CTFYS_COURSES)
    return get_config()


def _codes(res):
    return [u.rsplit("/", 1)[1] for u in res.course_urls]


def test_courses_named_in_the_sentence_are_both_fetched_without_asking(ctfys):
    q = (
        "Jag klarade inte första tentan i differetialekvationer och transformmetoder, "
        "kan jag fortfarande läsa reglerteknik?"
    )
    res = cr.resolve_course_intent(ctfys, q, program_prior="CTFYS")
    assert res is not None
    assert res.clarification_sv == ""
    assert _codes(res) == ["SF1683", "EL1000"]


def test_a_prerequisites_question_counts_as_course_intent(ctfys):
    q = "Vad har reglerteknik som förkunskapskrav? Det borde du kunna svara på?"
    assert cr.question_has_course_intent(q)
    res = cr.resolve_course_intent(ctfys, q, program_prior="CTFYS")
    assert res is not None and _codes(res) == ["EL1000"]


def test_a_shared_word_alone_does_not_name_a_course():
    tokens = set("hur går tentan i analys till".split())
    assert not cr._named_in_question("Analys i en variabel", tokens)
    assert cr._named_in_question("Analys i en variabel", tokens | {"variabel"})
    # Generic words never identify a course on their own.
    assert not cr._named_in_question("Allmän kurs", tokens | {"allmän", "kurs"})


def test_the_phrase_path_still_resolves_a_partial_name(ctfys):
    # Half the name: not "named in the question", so the phrase path scores it.
    q = "Vad krävs för kursen differentialekvationer?"
    assert not cr._named_in_question(
        "Differentialekvationer och transformmetoder", set(q.lower().split())
    )
    res = cr.resolve_course_intent(ctfys, q, program_prior="CTFYS")
    assert res is not None and _codes(res) == ["SF1683"]


def test_a_one_word_name_needs_an_exact_compound_word():
    assert cr._named_in_question("Reglerteknik, allmän kurs", {"läsa", "reglerteknik"})
    assert not cr._named_in_question("Reglerteknik, allmän kurs", {"reglertekniken"})
    assert not cr._named_in_question("Fysik I", {"tentan", "fysik"})
    assert not cr._named_in_question("Mekanik", {"kurs", "mekanik"})
    assert not cr._named_in_question("Datorteknik, grundkurs", {"kurser", "datateknik"})


def test_nothing_without_a_known_programme(ctfys):
    q = "Kan jag läsa reglerteknik?"
    assert cr.resolve_course_intent(ctfys, q) is None
