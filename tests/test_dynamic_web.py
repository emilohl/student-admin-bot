from __future__ import annotations

from pathlib import Path

import student_bot.bot.web_retrieval as wr
from student_bot.bot.web_cache import WebCache
from student_bot.bot.web_retrieval import (
    _canonicalize,
    _compiled_patterns,
    _extract_targets_with_cfg,
    _is_allowed_url,
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


def test_build_doc_url_keeps_absolute_web_urls():
    got = build_doc_url("https://www.kth.se/student/kurser/program/TNTEM", None, "/docs")
    assert got == "https://www.kth.se/student/kurser/program/TNTEM"

