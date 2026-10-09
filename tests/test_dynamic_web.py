from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest

import student_bot.bot.web_retrieval as wr
from student_bot.bot.web_cache import WebCache
from student_bot.bot.web_retrieval import (
    AdmissionHints,
    _canonicalize,
    _compiled_patterns,
    _explicit_unknown_programme_codes,
    _extract_targets_with_cfg,
    _is_allowed_url,
    _select_programme_urls,
    corpus_programme_substrings_for_query,
    history_without_programme_clarification_tail,
    is_programme_clarification_assistant_message,
    merge_programme_clarification_followup,
    parse_program_admission_hints,
    program_study_intent_question,
    reply_admission_hints,
)
from student_bot.bot.citations import build_doc_url
from student_bot.config import get_config


def test_allowlist_accepts_course_and_program_urls():
    cfg = get_config()
    patterns = _compiled_patterns(cfg)
    assert _is_allowed_url("https://www.kth.se/student/kurser/kurs/DD1331", cfg, patterns)
    assert _is_allowed_url("https://www.kth.se/student/kurser/program/CTFYS", cfg, patterns)
    assert _is_allowed_url(
        "https://www.kth.se/student/kurser/program/CTFYS/20232/arskurs3", cfg, patterns
    )
    assert _is_allowed_url("https://www.kth.se/student/kurser/program/CTFYS/20232", cfg, patterns)


def test_allowlist_rejects_nonmatching_urls():
    cfg = get_config()
    patterns = _compiled_patterns(cfg)
    assert not _is_allowed_url("https://www.kth.se/student/studier/examen", cfg, patterns)
    assert not _is_allowed_url("https://example.org/student/kurser/kurs/DD1331", cfg, patterns)


def test_canonicalize_drops_query_and_fragment():
    url = "https://www.kth.se/student/kurser/kurs/DD1331?foo=1#bar"
    assert _canonicalize(url) == "https://www.kth.se/student/kurser/kurs/DD1331"


def test_cache_age_days_never_negative():
    assert WebCache.age_days(9999999999) == 0


def test_cache_db_path_is_relative_to_project_root():
    cfg = get_config()
    cache = WebCache(cfg)
    assert cache._path == cfg.absolute(Path(cfg.dynamic_web.cache_db))


def test_explicit_unknown_programme_codes_when_not_in_kth_snapshot(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(
        wr,
        "_get_program_aliases",
        lambda _cfg: {"civilingenjorsutbildning i medieteknik": "CMETE"},
    )
    q = "Finns det ett program som heter CFUSK?"
    assert _explicit_unknown_programme_codes(q, cfg) == ["CFUSK"]


def test_explicit_unknown_programme_codes_empty_when_code_known(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(
        wr,
        "_get_program_aliases",
        lambda _cfg: {"civilingenjorsutbildning i medieteknik": "CMETE"},
    )
    q = "Finns programmet CMETE?"
    assert _explicit_unknown_programme_codes(q, cfg) == []


def test_explicit_unknown_programme_codes_requires_program_intent(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {"x": "CMETE"})
    assert _explicit_unknown_programme_codes("Vad betyder förkortningen CFUSK?", cfg) == []


def test_maybe_fetch_returns_missing_program_for_unknown_code(monkeypatch):
    cfg = get_config()
    if not cfg.dynamic_web.enabled:
        pytest.skip("dynamic web disabled")
    monkeypatch.setattr(
        wr,
        "_get_program_aliases",
        lambda _cfg: {"civilingenjorsutbildning i medieteknik": "CMETE"},
    )
    r = wr.maybe_fetch_dynamic_web(cfg, "Finns det ett program som heter CFUSK?", "sv")
    assert r is not None and r.missing_kth_program
    assert "CFUSK" in (r.missing_kth_program[0] + r.missing_kth_program[1])


@pytest.mark.parametrize(
    "override,year",
    [(None, "2019"), (AdmissionHints(), "2019"), (AdmissionHints(year_prefix="2022"), "2022")],
)
def test_maybe_fetch_takes_the_admission_override_over_the_text(monkeypatch, override, year):
    """The pipeline passes the admission year of the student's latest reply. It must win
    over an earlier year in the joined text. A missing admission year is passed on."""
    cfg = get_config()
    if not cfg.dynamic_web.enabled:
        pytest.skip("dynamic web disabled")
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {"ctfys": "CTFYS"})
    seen: list[AdmissionHints] = []

    def _root(_cfg, _url, hints, **_kw):
        seen.append(hints)
        return wr.ProgrammeRootResolution(
            queue_urls=[], clarification_sv="?", clarification_en="?", admission_not_found=True
        )

    monkeypatch.setattr(wr, "_resolve_program_root_targets", _root)
    q = "Vilka kurser är obligatoriska i årskurs 2 på CTFYS?\n\nHT2019"
    r = wr.maybe_fetch_dynamic_web(cfg, q, "sv", admission_hints_override=override)
    assert seen and seen[-1].year_prefix == year
    assert r is not None and r.clarification and r.admission_not_found


def test_extract_targets_uses_five_letter_program_codes(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {"teknisk fysik": "CTFYS"})
    q = "show me the study plan for CTFYS"
    out = _extract_targets_with_cfg(q, cfg)
    assert "https://www.kth.se/student/kurser/program/CTFYS" in out


def test_extract_targets_resolves_program_alias(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {"teknisk fysik": "CTFYS"})
    q = "Hur ser utbildningsplanen ut for teknisk fysik?"
    out = _extract_targets_with_cfg(q, cfg)
    assert "https://www.kth.se/student/kurser/program/CTFYS" in out


def test_parse_program_aliases_falls_back_to_compressed_store():
    """KTH programme list pages can omit /program/ links; codes live in compressed store."""
    payload = {
        "programmes": [
            [
                "CING",
                {
                    "first": [
                        {
                            "programmeCode": "CTFYS",
                            "title": "Civilingenjörsutbildning i teknisk fysik",
                            "titleOtherLanguage": "Master of Science in Engineering Physics",
                        }
                    ]
                },
            ]
        ]
    }
    from urllib.parse import quote

    enc = quote(json.dumps(payload, separators=(",", ":")))
    html = f'<html><head><title>Utbildningsplaner</title></head><body><script>window.__compressedApplicationStore__="{enc}";</script></body></html>'
    got = wr._parse_program_aliases_from_html(html)
    assert got.get("civilingenjörsutbildning i teknisk fysik") == "CTFYS"
    assert got.get("master of science in engineering physics") == "CTFYS"
    assert got.get("ctfys") == "CTFYS"


def test_extract_targets_ignores_term_codes_like_ht_yyyy():
    cfg = get_config()
    q = "Vad galler for kursval HT2024 och DD1331?"
    out = _extract_targets_with_cfg(q, cfg)
    assert "https://www.kth.se/student/kurser/kurs/DD1331" in out
    assert "https://www.kth.se/student/kurser/kurs/HT2024" not in out


def test_extract_targets_accepts_three_digit_suffix_letter_course_code():
    cfg = get_config()
    q = "Vilka krav galler for kandidatexamensarbete SK110X?"
    out = _extract_targets_with_cfg(q, cfg)
    assert "https://www.kth.se/student/kurser/kurs/SK110X" in out


def test_extract_targets_ignores_unknown_five_letter_token(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {"teknisk fysik": "CTFYS"})
    q = "Vad ar programkoden for FYSIK?"
    out = _extract_targets_with_cfg(q, cfg)
    assert "https://www.kth.se/student/kurser/program/FYSIK" not in out


def test_extract_targets_skips_bare_program_code_when_alias_index_has_no_known_codes(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {"some phrase": "NOT5L"})
    q = "Utbildningsplan för CMETE"
    out = _extract_targets_with_cfg(q, cfg)
    assert "https://www.kth.se/student/kurser/program/CMETE" in out


def test_extract_targets_does_not_match_generic_masterprogram_alias(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(
        wr,
        "_get_program_aliases",
        lambda _cfg: {
            "masterprogram, matematik": "TMAKM",
            "civilingenjorsutbildning i teknisk fysik": "CTFYS",
        },
    )
    # Isolate from curated nicknames so this test verifies alias scoring only.
    monkeypatch.setattr(wr, "_load_program_nicknames", lambda _cfg: {})
    q = "Vad ar programkoden for masterprogrammet i teknisk fysik?"
    out = _extract_targets_with_cfg(q, cfg)
    assert "https://www.kth.se/student/kurser/program/CTFYS" in out
    assert "https://www.kth.se/student/kurser/program/TMAKM" not in out


def test_extract_targets_prefers_multiword_program_alias_over_single_subject(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(
        wr,
        "_get_program_aliases",
        lambda _cfg: {
            "masterprogram, fysik": "PHYSX",
            "civilingenjorsutbildning i teknisk fysik": "CTFYS",
        },
    )
    # Isolate from curated nicknames so this test verifies alias scoring only.
    monkeypatch.setattr(wr, "_load_program_nicknames", lambda _cfg: {})
    q = "Vad har masterprogrammet i teknisk fysik for programkod?"
    out = _extract_targets_with_cfg(q, cfg)
    assert "https://www.kth.se/student/kurser/program/CTFYS" in out
    assert "https://www.kth.se/student/kurser/program/PHYSX" not in out


def test_extract_targets_teknisk_matematik_drops_master_math_via_query_coverage(monkeypatch):
    """Regression for #39 — query 'teknisk matematik' must not pull a master
    program that only matches 'matematik' through the alias scoring asymmetry.

    The civilingenjör alias has both 'teknisk' and 'matematik' as strong
    tokens (CTMAT); the master-math alias has only 'matematik'. Pre-fix, both
    scored 1.0 by alias-side coverage alone. With the query-coverage factor
    now multiplied in, the master alias drops to 0.5 (1 of 2 query strong
    tokens explained) — below the 0.6 threshold — and never reaches
    disambiguation."""
    cfg = get_config()
    monkeypatch.setattr(
        wr,
        "_get_program_aliases",
        lambda _cfg: {
            "civilingenjörsutbildning i teknisk matematik": "CTMAT",
            "masterprogram, matematik": "TMTHM",
        },
    )
    monkeypatch.setattr(wr, "_load_program_nicknames", lambda _cfg: {})
    out = _extract_targets_with_cfg("utbildningsplan för teknisk matematik", cfg)
    assert "https://www.kth.se/student/kurser/program/CTMAT" in out
    assert "https://www.kth.se/student/kurser/program/TMTHM" not in out


def test_extract_targets_teknisk_matematik_nickname_override(monkeypatch):
    """The curated nickname override in `data/program_nicknames.json` pins
    'teknisk matematik' to CTMAT even when other math-flavoured aliases
    happen to score above threshold."""
    cfg = get_config()
    monkeypatch.setattr(
        wr,
        "_get_program_aliases",
        lambda _cfg: {
            "civilingenjörsutbildning i teknisk matematik": "CTMAT",
            "masterprogram, ingenjörstillämpad matematik": "TITMM",
            # Add a same-token decoy so alias scoring alone would still surface
            # other candidates.
            "matematik och teknik": "FAKE1",
        },
    )
    monkeypatch.setattr(
        wr,
        "_load_program_nicknames",
        lambda _cfg: {"teknisk matematik": ["CTMAT"]},
    )
    out = _extract_targets_with_cfg("utbildningsplan för teknisk matematik", cfg)
    assert "https://www.kth.se/student/kurser/program/CTMAT" in out
    # Other math-flavoured candidates should not survive the nickname override.
    assert "https://www.kth.se/student/kurser/program/TITMM" not in out
    assert "https://www.kth.se/student/kurser/program/FAKE1" not in out


def test_extract_targets_recency_penalty_drops_discontinued_program(monkeypatch):
    """A historical program (no intake since 2012) gets its alias score
    halved and falls below the 0.6 threshold, even when the alias scoring
    alone would otherwise rank it equally with a current civilingenjör
    candidate. Two aliases that both match the query's strong tokens fully:
    only recency should distinguish them."""
    cfg = get_config()
    monkeypatch.setattr(
        wr,
        "_get_program_aliases",
        lambda _cfg: {
            "civilingenjörsutbildning i teknisk matematik": "CTMAT",
            # Hypothetical alias that matches the query exactly the same way
            # as the CTMAT alias — both have strong tokens {teknisk, matematik}
            # and score 1.0 against "teknisk matematik". Without recency,
            # this would force a disambiguation prompt.
            "masterprogram i teknisk matematik": "TMTHM",
        },
    )
    monkeypatch.setattr(wr, "_load_program_nicknames", lambda _cfg: {})
    # Stub the term fetcher: CTMAT current, TMTHM last took students in 2012.
    monkeypatch.setattr(
        wr,
        "_cached_terms_for_code",
        lambda _cfg, code: {"CTMAT": ["20242"], "TMTHM": ["20122"]}.get(code, []),
    )
    out = _extract_targets_with_cfg("utbildningsplan för teknisk matematik", cfg)
    assert "https://www.kth.se/student/kurser/program/CTMAT" in out
    assert "https://www.kth.se/student/kurser/program/TMTHM" not in out


def test_discriminator_rare_token_required_for_historical_rescue(monkeypatch):
    """Historical-program rescue in disambiguation requires a *rare*
    matched token (≤ 3 aliases). 'matematik' is common (4 aliases here) so
    a discontinued master with only that token should NOT be rescued."""
    cfg = get_config()
    aliases = {
        "civilingenjörsutbildning i teknisk matematik": "CTMAT",
        "masterprogram, matematik": "TMTHM",
        "masterprogram, ingenjörstillämpad matematik": "TITMM",
        "masterprogram, tillämpad matematik och beräkningsmatematik": "TTMAM",
        # 4 aliases all contain "matematik" → not rare → not a rescue token.
    }
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: aliases)
    # Stub terms: CTMAT current, TMTHM historical (last 2012).
    monkeypatch.setattr(
        wr,
        "_cached_terms_for_code",
        lambda _cfg, code: {"CTMAT": ["20242"], "TMTHM": ["20122"]}.get(code, []),
    )
    resolution = wr._resolve_multi_program_candidates(
        cfg,
        "teknisk matematik",
        [
            "https://www.kth.se/student/kurser/program/CTMAT",
            "https://www.kth.se/student/kurser/program/TMTHM",
        ],
    )
    # CTMAT current + TMTHM historical → without a rare discriminator,
    # TMTHM is suppressed and the resolver returns just CTMAT.
    assert resolution.resolved_code == "CTMAT"


def test_build_doc_url_keeps_absolute_web_urls():
    got = build_doc_url("https://www.kth.se/student/kurser/program/TNTEM", None, "/docs")
    assert got == "https://www.kth.se/student/kurser/program/TNTEM"


def test_parse_admission_hints_five_digit_priority():
    h = parse_program_admission_hints("study plan CTFYS 20242 cohort")
    assert h.exact_term == "20242"
    assert h.year_prefix is None


def test_parse_admission_hints_ht_year():
    h = parse_program_admission_hints("utbildningsplan för CTFYS HT2026")
    assert h.year_prefix == "2026"


@pytest.mark.parametrize(
    "question,year",
    [
        ("Vilka kurser läser man i årskurs 2 på CTFYS, kull HT22?", "2022"),
        ("utbildningsplan för CTFYS ht-22", "2022"),
        ("Studieplan för CTFYS VT'23", "2023"),
        ("Vilka kurser har CTFYS höstterminen 2022?", "2022"),
        ("Which courses does CTFYS have in the spring semester 2023?", "2023"),
        ("CTFYS courses for the autumn 2022 intake", "2022"),
        # Two digits after a space are credits, not a year.
        ("Kan jag läsa HT 15 hp och VT 15 hp?", None),
        # "fall" is not a semester word: Swedish "i så fall".
        ("Vad gäller i så fall 2026?", None),
    ],
)
def test_parse_admission_hints_two_digit_and_semester_words(question, year):
    assert parse_program_admission_hints(question).year_prefix == year


def test_corpus_hints_only_when_program_intent():
    assert corpus_programme_substrings_for_query("HT2024 och DD1331") is None
    assert program_study_intent_question("course DD1331") is False
    s = corpus_programme_substrings_for_query("utbildningsplan CTFYS HT2024")
    assert s is not None and "2024" in s


def test_select_programme_urls_single_term_without_hints():
    r = _select_programme_urls("CTFYS", ["20242"], AdmissionHints())
    assert r.queue_urls == ["https://www.kth.se/student/kurser/program/CTFYS/20242"]


def test_select_programme_urls_clarifies_when_ambiguous_without_hints():
    r = _select_programme_urls("CTFYS", ["20252", "20242"], AdmissionHints())
    assert r.queue_urls == []
    assert "2024" in r.clarification_sv and "2025" in r.clarification_sv
    assert "2024" in r.clarification_en and "2025" in r.clarification_en


def test_select_programme_urls_filters_by_year_hint():
    r = _select_programme_urls(
        "CTFYS",
        ["20262", "20252", "20242"],
        AdmissionHints(year_prefix="2026"),
    )
    assert r.queue_urls == ["https://www.kth.se/student/kurser/program/CTFYS/20262"]


def test_select_programme_urls_explicit_term():
    r = _select_programme_urls(
        "CTFYS",
        ["20252", "20242"],
        AdmissionHints(exact_term="20252"),
    )
    assert r.queue_urls[0].endswith("/20252")


def test_parse_admission_hints_started_year_sv():
    h = parse_program_admission_hints("Jag började 2025")
    assert h.year_prefix == "2025"


def test_merge_programme_clarification_followup_with_history():
    hist = [
        {"role": "user", "content": "Vilka kurser ingår i år 2 av programmet CTMAT?"},
        {
            "role": "assistant",
            "content": (
                "För att visa rätt utbildningsplan för **CTMAT** behöver jag veta "
                "vilken antagningsomgång som gäller."
            ),
        },
    ]
    merged = merge_programme_clarification_followup("Jag började 2025", hist)
    assert "CTMAT" in merged and "år 2" in merged and "Jag började 2025" in merged


def test_merge_programme_followup_bare_year():
    hist = [
        {"role": "user", "content": "Utbildningsplan för CDATE"},
        {
            "role": "assistant",
            "content": "vilken antagningsomgång som gäller — ange HT2024 eller VT2025.",
        },
    ]
    merged = merge_programme_clarification_followup("2026", hist)
    assert "CDATE" in merged and "2026" in merged


def test_merge_uses_the_pending_question_after_a_second_clarification():
    """Programme pick, then admission year: the nearest user message is the
    first reply ("CTFYS"), so without the saved question the ask is lost."""
    hist = [
        {"role": "user", "content": "Vilka spärrkurser finns i årskurs 2?"},
        {"role": "assistant", "content": "Ditt program är inte entydigt. Vilket menar du?"},
        {"role": "user", "content": "CTFYS"},
        {"role": "assistant", "content": "vilken antagningsomgång som gäller för dig?"},
    ]
    pending = "Vilka spärrkurser finns i årskurs 2?\n\nCTFYS"
    merged = merge_programme_clarification_followup("HT2022", hist, pending_question=pending)
    assert merged == f"{pending}\n\nHT2022"


def test_merge_ignores_the_pending_question_when_the_reply_is_not_an_answer():
    hist = [
        {"role": "user", "content": "Utbildningsplan för CDATE"},
        {"role": "assistant", "content": "vilken antagningsomgång som gäller för dig?"},
    ]
    question = "Vad är CSN?"
    merged = merge_programme_clarification_followup(
        question, hist, pending_question="Utbildningsplan för CDATE"
    )
    assert merged == question


def test_a_year_the_programme_has_no_admission_for_is_reported():
    """The pipeline keeps such an admission year out of memory, so the router says so."""
    terms = ["20242", "20232", "20222"]
    missing = _select_programme_urls("CTFYS", terms, AdmissionHints(year_prefix="2030"))
    found = _select_programme_urls("CTFYS", terms, AdmissionHints(year_prefix="2023"))
    assert missing.clarification_sv and missing.admission_not_found
    assert found.queue_urls and not found.admission_not_found


@pytest.mark.parametrize("terms", [["20242", "20232", "20222"], ["20242"]])
def test_a_five_digit_term_the_programme_does_not_have_is_reported(terms):
    """Asked like a year with no admission. It used to say "several rounds match
    the year None", or route a programme with one admission year to its only plan."""
    r = _select_programme_urls("CTFYS", terms, AdmissionHints(exact_term="20192"))
    assert not r.queue_urls and r.admission_not_found
    assert "20192" in r.clarification_sv and "None" not in r.clarification_sv
    # The standard admission year question, so the student's next reply is joined.
    assert is_programme_clarification_assistant_message(r.clarification_sv)
    assert is_programme_clarification_assistant_message(r.clarification_en)


@pytest.mark.parametrize(
    "reply,admission",
    [
        ("HT2022", (None, "2022")),
        ("VT2022", (None, "2022")),
        ("hösten 2022", (None, "2022")),
        ("jag började 2022", (None, "2022")),
        ("2022", (None, "2022")),
        ("20222", ("20222", None)),
        ("Vad är CSN?", (None, None)),
        # A reply that names one year and is not a question, however it is phrased.
        ("år 2023 var det", (None, "2023")),
        ("2023 tror jag", (None, "2023")),
        ("2023.", (None, "2023")),
        ("2023?", (None, "2023")),
        ("i 2023", (None, "2023")),
        ("2023, ja 2023", (None, "2023")),
        ("Jag tror att jag kom in 2025", (None, "2025")),
        ("Det borde ha varit 2025 tror jag", (None, "2025")),
        ("Jag är inte helt säker men 2025", (None, "2025")),
        ("Antogs 2025 tror jag", (None, "2025")),
        # An explicit form counts despite a "?", also with a question after it.
        ("HT2023?", (None, "2023")),
        ("hösten 2023?", (None, "2023")),
        ("HT2022, vilka kurser blir det då?", (None, "2022")),
        ("Jag började HT2022, vilka kurser har jag i årskurs 2?", (None, "2022")),
        # A question word with no "?" can open an answer.
        ("När jag började var det HT2022", (None, "2022")),
        ("Vad jag minns började jag 2022", (None, "2022")),
        ("When I started it was 2022", (None, "2022")),
        # HT/VT with two digits, and the semester words.
        ("HT22", (None, "2022")),
        ("ht-22", (None, "2022")),
        ("VT'23", (None, "2023")),
        ("höstterminen 2022", (None, "2022")),
        ("vårterminen 2023", (None, "2023")),
        ("autumn 2022", (None, "2022")),
        ("spring semester 2023", (None, "2023")),
        # A reply that starts with a question word (whatever form its year has),
        # a bare year in a question, two different years, or two digits after a
        # space: none.
        ("Hur mycket CSN fick man år 2023?", (None, None)),
        ("När är omtentan 2025?", (None, None)),
        ("När är omtentan VT2026?", (None, None)),
        ("Kan jag söka utbyte 2026?", (None, None)),
        ("Vilka kurser gick man 2023", (None, None)),
        ("2021 eller 2023", (None, None)),
        ("ht 22", (None, None)),
    ],
)
def test_reply_admission_hints(reply, admission):
    hints = reply_admission_hints(reply)
    assert (hints.exact_term, hints.year_prefix) == admission


def test_history_without_programme_clarification_tail():
    hist = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "vilken antagningsomgång som gäller för dig?"},
    ]
    trimmed = history_without_programme_clarification_tail(hist, programme_followup_merged=True)
    assert trimmed == []


def test_programme_page_merges_courses_from_compressed_store():
    """Study-plan listings often live only in embedded JSON — not in p/li text."""
    payload = {"courses": [{"courseCode": "BB1190", "titleSv": "Introduktion till bioteknik"}]}
    enc = quote(json.dumps(payload, separators=(",", ":")))
    html = (
        "<html><body><h1>CBIOT år 1</h1><p>kort stub</p>"
        f'<script>window.__compressedApplicationStore__="{enc}";</script></body></html>'
    )
    _, body = wr._sanitize_to_text(html)
    assert "BB1190" not in body
    merged = wr._programme_page_text_with_store(html, body)
    assert "BB1190" in merged
    assert "Introduktion" in merged


def test_programme_page_store_fallback_extracts_codes_without_titles():
    payload = {"items": [{"code": "AB1234"}]}
    enc = quote(json.dumps(payload, separators=(",", ":")))
    html = (
        "<html><body><h1>Test</h1>"
        f'<script>window.__compressedApplicationStore__="{enc}";</script></body></html>'
    )
    merged = wr._programme_page_text_with_store(html, "")
    assert "AB1234" in merged


def test_sanitize_includes_table_rows():
    html = "<html><body><table><tr><th>Kod</th><th>Namn</th></tr><tr><td>XX1001</td><td>Foo</td></tr></table></body></html>"
    _, body = wr._sanitize_to_text(html)
    assert "XX1001" in body and "Foo" in body


def test_programme_store_groups_obligatory_and_elective_buckets():
    """Utbildningsplan JSON uses Valvillkor (O, VV, …); keep headings in plaintext."""
    payload = {
        "curriculums": [
            {
                "studyYears": [
                    {
                        "yearNumber": 2,
                        "freeTexts": [],
                        "courses": [
                            {
                                "kod": "EH1110",
                                "benamning": "Obligatorisk testkurs",
                                "Valvillkor": "O",
                                "omfattning": {"number": 7.5, "formattedWithUnit": "7,5 hp"},
                            },
                            {
                                "kod": "DD1320",
                                "benamning": "Valbar testkurs",
                                "Valvillkor": "VV",
                                "omfattning": {"number": 6.0, "formattedWithUnit": "6,0 hp"},
                            },
                        ],
                    }
                ]
            }
        ]
    }
    enc = quote(json.dumps(payload, separators=(",", ":")))
    html = (
        "<html><body><h1>Stub</h1>"
        f'<script>window.__compressedApplicationStore__="{enc}";</script></body></html>'
    )
    url = "https://www.kth.se/student/kurser/program/FAKE1/20252/arskurs2"
    merged = wr._programme_page_text_with_store(html, "", url)
    assert "Obligatoriska kurser" in merged
    assert "Valbara kurslistor" in merged and "villkorligt valbara" in merged
    assert "EH1110" in merged and "DD1320" in merged
    assert merged.index("EH1110") < merged.index("DD1320")
    assert "7,5 hp" in merged and "6,0 hp" in merged


def test_kth_unknown_course_placeholder_detected():
    assert wr._is_kth_placeholder_course_shell("undefined undefined undefined")
    assert not wr._is_kth_placeholder_course_shell("SF1625 Envariabelanalys")
    assert (
        wr._kth_course_code_from_course_url("https://www.kth.se/student/kurser/kurs/SE1050")
        == "SE1050"
    )


def test_kth_unknown_programme_title_shell_detected():
    assert wr._programme_root_title_is_unknown_code_shell(
        "CFUSK (CFUSK), Utbildningsplaner |\xa0KTH"
    )
    assert wr._programme_root_title_is_unknown_code_shell("XXXXX (XXXXX), Utbildningsplaner | KTH")
    assert not wr._programme_root_title_is_unknown_code_shell(
        "Civilingenjörsutbildning i medieteknik (CMETE), Utbildningsplaner |\xa0KTH"
    )
