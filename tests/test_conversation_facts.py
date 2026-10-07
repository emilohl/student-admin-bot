"""The model sees what the conversation has established (programme, admission year).

`ConversationMemory` keeps both per thread, and the router has always read them
to pick a study plan. The model did not: once the turn that said "jag läser
CTFYS, började HT2024" left the history, the router still knew and the model
did not.
"""

from __future__ import annotations

import pytest

from student_bot.bot import web_retrieval as wr
from student_bot.bot.pipeline import _conversation_facts
from student_bot.bot.prompts import compose_messages, compose_meta_fallback_messages
from student_bot.config import get_config

# alias -> code, shaped like data/program_aliases.json (see test_programme_code_answer.py).
_ALIASES = {
    "ctfys": "CTFYS",
    "civilingenjörsutbildning i teknisk fysik": "CTFYS",
    "degree programme in engineering physics": "CTFYS",
}


@pytest.fixture(autouse=True)
def _no_network_alias_table(monkeypatch):
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: dict(_ALIASES))


@pytest.fixture(scope="module")
def cfg():
    return get_config()


def test_programme_and_admission_year_in_swedish(cfg):
    facts = _conversation_facts(cfg, "sv", "CTFYS", "20242", "2024")
    assert "program CTFYS (Civilingenjörsutbildning i teknisk fysik)" in facts
    assert "antagningsomgång HT2024" in facts
    assert "säger frågan något annat gäller frågan" in facts


def test_programme_name_follows_the_language(cfg):
    facts = _conversation_facts(cfg, "en", "CTFYS", None, None)
    assert "programme CTFYS (Degree programme in engineering physics)" in facts
    assert "the question wins" in facts


def test_a_year_alone_is_named_as_a_year(cfg):
    assert "antagningsår 2024" in _conversation_facts(cfg, "sv", None, None, "2024")


def test_an_unknown_code_is_shown_without_a_name(cfg):
    assert "program XXXXX." in _conversation_facts(cfg, "sv", "XXXXX", None, None)


def test_nothing_known_adds_nothing(cfg):
    assert _conversation_facts(cfg, "sv", None, None, None) == ""


def test_the_facts_open_the_user_message(cfg):
    facts = "Från samtalet hittills: program CTFYS."
    content = compose_messages(cfg, "sv", [], [], "Vilka kurser?", facts=facts)[-1]["content"]
    assert content.startswith(facts)
    assert content.endswith("Vilka kurser?")


def test_the_meta_fallback_gets_them_too(cfg):
    facts = "Från samtalet hittills: program CTFYS."
    content = compose_meta_fallback_messages(cfg, "sv", [], "Vad läser jag?", facts=facts)[-1][
        "content"
    ]
    assert content.startswith(facts) and content.endswith("Vad läser jag?")


def test_without_facts_the_messages_are_unchanged(cfg):
    assert compose_messages(cfg, "sv", [], [], "q") == compose_messages(
        cfg, "sv", [], [], "q", facts=""
    )
    meta = compose_meta_fallback_messages(cfg, "sv", [], "q")
    assert meta[-1] == {"role": "user", "content": "q"}
