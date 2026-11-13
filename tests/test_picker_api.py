"""The programme / admission-year picker's API: GET /api/programs feeds the
two selects, and /api/chat carries their values into the turn's priors.

A picker change replaces what memory holds, as if the student had written it,
so a student who picks their year is not asked for it; a hint written in the
question still wins further down, inside fetch_dynamic_web, and sticks.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from student_bot.bot.memory import ConversationMemory
from student_bot.config import PROJECT_ROOT, get_config
from student_bot.web import app as web_app

# Shaped like data/program_aliases.json: English catalogue first, Swedish second.
_ALIASES = {
    "degree programme in engineering physics": "CTFYS",
    "ctfys": "CTFYS",
    "degree programme in computer science and engineering": "CDATE",
    "cdate": "CDATE",
    "civilingenjörsutbildning i teknisk fysik": "CTFYS",
    "civilingenjörsutbildning i datateknik": "CDATE",
}


@pytest.fixture
def cfg(tmp_path):
    c = get_config().model_copy(deep=True)
    c.paths.logs_db = tmp_path / "logs.sqlite"
    c.web.auth_enabled = False
    return c


@pytest.fixture
def memory(cfg, monkeypatch):
    """The app's conversation memory, so a test can see what the picker did to it."""
    mem = ConversationMemory(cfg)
    monkeypatch.setattr(web_app, "ConversationMemory", lambda _cfg: mem)
    return mem


@pytest.fixture
def client(cfg, memory):
    return TestClient(web_app.create_app(cfg))


def test_programs_lists_each_code_once_with_both_names_sorted_by_code(cfg, client, tmp_path):
    path = tmp_path / "program_aliases.json"
    path.write_text(json.dumps({"fetched_at": 0, "aliases": _ALIASES}), encoding="utf-8")
    cfg.dynamic_web.program_aliases_file = str(path)

    resp = client.get("/api/programs")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "code": "CDATE",
            "name_sv": "Civilingenjörsutbildning i datateknik",
            "name_en": "Degree programme in computer science and engineering",
        },
        {
            "code": "CTFYS",
            "name_sv": "Civilingenjörsutbildning i teknisk fysik",
            "name_en": "Degree programme in engineering physics",
        },
    ]


def test_a_single_title_names_both_languages_and_none_the_code(cfg, client, tmp_path):
    """Two codes sharing a title keep it under one of them only, so a code can
    be left with its title in either language: TIEMM's Swedish one belongs to
    TIMIM in the real cache, CELTE's English one to TIELF."""
    path = tmp_path / "program_aliases.json"
    aliases = {
        "cmete": "CMETE",
        "civilingenjörsutbildning i elektroteknik": "CELTE",
        "master's programme, industrial engineering and management": "TIEMM",
    }
    path.write_text(json.dumps({"fetched_at": 0, "aliases": aliases}), encoding="utf-8")
    cfg.dynamic_web.program_aliases_file = str(path)

    celte = "Civilingenjörsutbildning i elektroteknik"
    tiemm = "Master's programme, industrial engineering and management"
    cfg.study_plan_cache.programmes = ["CELTE", "CMETE", "TIEMM"]
    assert client.get("/api/programs").json() == [
        {"code": "CELTE", "name_sv": celte, "name_en": celte},
        {"code": "CMETE", "name_sv": "CMETE", "name_en": "CMETE"},
        {"code": "TIEMM", "name_sv": tiemm, "name_en": tiemm},
    ]


def test_programs_is_empty_when_the_cache_is_missing(cfg, client, tmp_path):
    cfg.dynamic_web.program_aliases_file = str(tmp_path / "missing.json")
    resp = client.get("/api/programs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_programs_lists_only_the_configured_civilingenjor_programmes(cfg, client, tmp_path):
    path = tmp_path / "program_aliases.json"
    aliases = dict(_ALIASES, **{"master's programme, engineering physics": "TTFYM"})
    path.write_text(json.dumps({"fetched_at": 0, "aliases": aliases}), encoding="utf-8")
    cfg.dynamic_web.program_aliases_file = str(path)
    cfg.study_plan_cache.programmes = ["CTFYS", "CDATE"]
    assert [p["code"] for p in client.get("/api/programs").json()] == ["CDATE", "CTFYS"]


def test_programs_reads_the_real_alias_cache(cfg, client):
    if not (PROJECT_ROOT / "data" / "program_aliases.json").exists():
        pytest.skip("data/program_aliases.json is a gitignored cache")
    programs = client.get("/api/programs").json()
    codes = [p["code"] for p in programs]
    assert codes == sorted(set(codes))
    assert set(codes) <= set(cfg.study_plan_cache.programmes)
    assert all(c.startswith("C") for c in codes)
    cdate = next(p for p in programs if p["code"] == "CDATE")
    assert cdate["name_sv"] == "Civilingenjörsutbildning i datateknik"
    assert cdate["name_en"] == "Degree programme in computer science and engineering"


def test_programs_needs_the_same_access_as_the_other_endpoints(cfg, monkeypatch):
    monkeypatch.setenv("WEB_ACCESS_TOKEN", "tok")
    cfg.web.auth_enabled = True
    assert TestClient(web_app.create_app(cfg)).get("/api/programs").status_code == 403


@pytest.mark.parametrize(
    "field", [{"program_code": "abc"}, {"program_code": "CDATE1"}, {"admission_year": "24"}]
)
def test_chat_rejects_a_malformed_picker_value(client, field):
    resp = client.post("/api/chat", json={"question": "Vilka kurser läser jag?", **field})
    assert resp.status_code == 422


_USER = web_app._web_user_id(web_app.ChatRequest(question="q"), None)
_PRIORS = ("program_prior", "admission_term_prior", "admission_year_prefix_prior")


@pytest.fixture
def captured(monkeypatch, memory):
    """The priors /api/chat passes to `answer`, one tuple per turn, with memory
    holding CTFYS and HT2021 from earlier turns."""
    memory.set_program_code(_USER, "default", "CTFYS")
    memory.set_admission_hints(_USER, "default", exact_term="20212")
    calls: list[tuple] = []

    def fake_answer(question, **kwargs):
        calls.append(tuple(kwargs[k] for k in _PRIORS))
        raise RuntimeError("captured")

    monkeypatch.setattr(web_app, "answer", fake_answer)
    return calls


def _ask(client, program_code=None, admission_year=None):
    body = {"question": "Vilka kurser?", "program_code": program_code}
    resp = client.post("/api/chat", json={**body, "admission_year": admission_year})
    assert resp.status_code == 200


def test_the_picker_replaces_the_programme_and_year_from_memory(client, captured):
    _ask(client, "CDATE", "2024")
    assert captured == [("CDATE", None, "2024")]


def test_an_empty_picker_leaves_the_memory_priors_alone(client, captured):
    _ask(client)
    assert captured == [("CTFYS", "20212", None)]


def test_a_year_stated_after_a_picker_change_sticks(client, captured, memory):
    """The picker counts when it changes, like a year written in the question;
    the same value on the next turn does not undo the student's later reply."""
    _ask(client, admission_year="2019")
    # The student's reply to "no round starts with 2019", stored by remember_turn.
    memory.set_admission_hints(_USER, "default", exact_term="20222", replace=True)
    _ask(client, admission_year="2019")
    assert captured == [("CTFYS", None, "2019"), ("CTFYS", "20222", None)]


def test_clearing_the_year_picker_goes_back_to_the_newest_round(client, captured, memory):
    _ask(client, admission_year="2022")
    # What remember_turn stores for a turn answered from the picked year.
    memory.set_admission_hints(_USER, "default", year_prefix="2022", replace=True)
    _ask(client)
    assert captured[-1] == ("CTFYS", None, None)


@pytest.mark.parametrize("term,note", [("20242", "antagna HT2024"), (None, "")])
def test_meta_carries_the_defaulted_term_line(cfg, client, monkeypatch, term, note):
    """The client rebuilds the answer from numbered_body, dropping the streamed
    tail, so the newest-term line has to travel in meta too."""
    from student_bot.bot.gate import GateDecision
    from student_bot.bot.pipeline import AnswerResult
    from student_bot.bot.retrieval import RetrievalResult

    cfg.topics.enabled = False
    result = AnswerResult(
        question="q",
        lang="sv",
        answered=True,
        answer="Svar",
        rendered="Svar",
        gate=GateDecision(True, "web_live", 3.5, 3.5, 1),
        retrieval=RetrievalResult(query="q"),
        latency_ms=0,
        defaulted_admission_term=term,
    )
    monkeypatch.setattr(web_app, "answer", lambda *_a, **_k: result)

    stream = client.post("/api/chat", json={"question": "Vilka kurser?"}).text
    meta = json.loads(stream.split("event: meta\ndata: ", 1)[1].split("\n", 1)[0])

    assert note in meta["defaulted_term_note"]
    assert bool(meta["defaulted_term_note"]) is bool(term)


@pytest.mark.parametrize(
    "reason,merged,pending",
    [
        ("programme_clarification", None, "Vilka kurser?"),
        ("programme_clarification", "Q\n\nCTFYS", "Q\n\nCTFYS"),
        ("web_live", None, ""),
    ],
)
def test_meta_carries_the_question_a_clarification_waits_on(
    cfg, client, monkeypatch, reason, merged, pending
):
    """What remember_turn stores as pending, so the picker can send it again."""
    from student_bot.bot.gate import GateDecision
    from student_bot.bot.pipeline import AnswerResult
    from student_bot.bot.retrieval import RetrievalResult

    cfg.topics.enabled = False
    result = AnswerResult(
        question="Vilka kurser?",
        lang="sv",
        answered=False,
        answer="Vilket program läser du?",
        rendered="Vilket program läser du?",
        gate=GateDecision(False, reason, 0.0, 0.0, 0),
        retrieval=RetrievalResult(query="q"),
        latency_ms=0,
        merged_question=merged,
    )
    monkeypatch.setattr(web_app, "answer", lambda *_a, **_k: result)
    stream = client.post("/api/chat", json={"question": "Vilka kurser?"}).text
    meta = json.loads(stream.split("event: meta\ndata: ", 1)[1].split("\n", 1)[0])
    assert meta["pending_question"] == pending


def test_a_picker_that_agrees_with_memory_changes_nothing(memory):
    """The client syncs the pickers to what the answer settled on."""
    memory.set_program_code("u", "t", "CDATE")
    memory.set_admission_hints("u", "t", exact_term="20231")
    memory.apply_picker("u", "t", "CDATE", "2023")
    assert memory.get_program_code("u", "t") == "CDATE"
    assert memory.get_admission_hints("u", "t") == ("20231", None)


def test_switching_programme_keeps_the_year_but_not_the_term(memory):
    memory.apply_picker("u", "t", "CTFYS", None)
    memory.set_admission_hints("u", "t", exact_term="20212")
    memory.apply_picker("u", "t", "CDATE", None)
    assert memory.get_program_code("u", "t") == "CDATE"
    assert memory.get_admission_hints("u", "t") == (None, "2021")


@pytest.mark.parametrize(
    "answer_text,kind",
    [
        ("Vilket program läser du? Skriv programkoden.", "programme"),
        ("Ditt program är inte entydigt. Vilket menar du?", "programme"),
        (
            "För att visa rätt utbildningsplan behöver jag veta vilken antagningsomgång som gäller.",
            "year",
        ),
        ("Du nämnde inte någon kurskod. Vilken menar du?", ""),
    ],
)
def test_meta_says_which_picker_answers_the_clarification(
    cfg, client, monkeypatch, answer_text, kind
):
    from student_bot.bot.gate import GateDecision
    from student_bot.bot.pipeline import AnswerResult
    from student_bot.bot.retrieval import RetrievalResult

    cfg.topics.enabled = False
    result = AnswerResult(
        question="q",
        lang="sv",
        answered=False,
        answer=answer_text,
        rendered=answer_text,
        gate=GateDecision(False, "programme_clarification", 0.0, 0.0, 0),
        retrieval=RetrievalResult(query="q"),
        latency_ms=0,
        program_code="CTFYS",
        admission_term="20242",
    )
    monkeypatch.setattr(web_app, "answer", lambda *_a, **_k: result)
    stream = client.post("/api/chat", json={"question": "q"}).text
    meta = json.loads(stream.split("event: meta\ndata: ", 1)[1].split("\n", 1)[0])
    assert meta["pending_kind"] == kind
    assert (meta["program_code"], meta["admission_year"]) == ("CTFYS", "2024")
