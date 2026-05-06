from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import student_bot.bot.web_retrieval as wr
from student_bot.bot.web_cache import WebCache
from student_bot.bot.web_retrieval import (
    AdmissionHints,
    _canonicalize,
    _compiled_patterns,
    _extract_targets_with_cfg,
    _is_allowed_url,
    _select_programme_urls,
    corpus_programme_substrings_for_query,
    history_without_programme_clarification_tail,
    merge_programme_clarification_followup,
    parse_program_admission_hints,
    program_study_intent_question,
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


def test_extract_targets_uses_five_letter_program_codes():
    cfg = get_config()
    q = "show me the study plan for CTFYS"
    out = _extract_targets_with_cfg(q, cfg)
    assert "https://www.kth.se/student/kurser/program/CTFYS" in out


def test_extract_targets_resolves_program_alias(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {"teknisk fysik": "CTFYS"})
    q = "Hur ser utbildningsplanen ut for teknisk fysik?"
    out = _extract_targets_with_cfg(q, cfg)
    assert "https://www.kth.se/student/kurser/program/CTFYS" in out


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
            "fysik": "FYSIK",
            "civilingenjorsutbildning i teknisk fysik": "CTFYS",
        },
    )
    q = "Vad har masterprogrammet i teknisk fysik for programkod?"
    out = _extract_targets_with_cfg(q, cfg)
    assert "https://www.kth.se/student/kurser/program/CTFYS" in out
    assert "https://www.kth.se/student/kurser/program/FYSIK" not in out


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
