"""Programme course lists and kursplan eligibility, cached for backward
prerequisite questions ("jag klarade inte X, vilka kurser spärrar den?", #136).

KTH publishes what each course requires, never what it unlocks, so answering
means reading the *Särskild behörighet* of every course in a programme. That
takes minutes per programme, so the weekly scrape runs `warm_study_plans` and
stores the extracted data as JSON in `WebCache` under `cache_key(url)`. The
question path must only ever read that cache, never fetch.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from student_bot.bot.web_cache import CachedPage, WebCache
from student_bot.bot.web_retrieval import (
    latest_cohort,
    _compiled_patterns,
    _compressed_application_store,
    _decode_kth_state_blob,
    _fetch_html,
    _is_allowed_url,
    _strip_html_to_text,
)
from student_bot.config import Config

log = logging.getLogger("student_bot")

COURSE_PAGE_URL = "https://www.kth.se/student/kurser/kurs/{code}"
PROGRAMME_YEAR_URL = "https://www.kth.se/student/kurser/program/{code}/{term}/arskurs{year}"

# Course pages carry the same kind of state blob as programme pages, under a
# different variable.
_COURSE_PAGE_STATE_RE = re.compile(
    r'window\.__compressedData__DATA\s*=\s*"([^"]+)"\s*;?',
    re.DOTALL,
)

# Participation conditions on a year page: obligatorisk, villkorligt valfri,
# valfri, rekommenderad.
_CONDITIONS = ("O", "VV", "V", "R")


def cache_key(url: str) -> str:
    """`WebCache` key for a page's extracted data. The dynamic-web path caches
    sanitized text under the bare URL, so the two never collide."""
    return f"{url}#study-plan"


def programme_year_url(code: str, cohort: int, year: int) -> str:
    """Study year `year` for the cohort admitted in the autumn of `cohort`."""
    return PROGRAMME_YEAR_URL.format(code=code, term=cohort * 10 + 2, year=year)


def _first_period(credits_per_period: object) -> int | None:
    """First teaching period (1-4) with credits in KTH's six-slot
    `creditsPerPeriod`; `None` when the course is not scheduled yet."""
    if not isinstance(credits_per_period, list) or len(credits_per_period) != 6:
        return None
    for period in (1, 2, 3, 4):
        try:
            if float(str(credits_per_period[period]).replace(",", ".")) > 0:
                return period
        except ValueError:
            continue
    return None


def parse_programme_year(html: str) -> list[dict] | None:
    """Courses a year page lists: `{code, name, condition, first_period}`.
    `[]` is a readable page without courses (KTH stops listing past years,
    lists years 4-5 of C-programmes under the master tracks, and answers a
    term without admission with its own `statusCode` 404); `None` means the
    page could not be read."""
    state = _compressed_application_store(html) or {}
    if state.get("statusCode") == 404:
        return []
    infos = state.get("curriculumInfos")
    if not isinstance(infos, list):
        return None
    courses = []
    for info in infos:
        participations = info.get("participations") if isinstance(info, dict) else None
        if not isinstance(participations, dict):
            continue
        for condition, entries in participations.items():
            if condition not in _CONDITIONS:
                continue
            for part in entries or []:
                course = part.get("course") or {}
                if isinstance(course.get("courseCode"), str):
                    courses.append(
                        {
                            "code": course["courseCode"],
                            "name": str(course.get("title") or "").strip(),
                            "condition": condition,
                            "first_period": _first_period(part.get("creditsPerPeriod")),
                        }
                    )
    return courses


def parse_course_page(html: str) -> list[dict] | None:
    """Kursplan versions `{term, eligibility}`, newest first. `term` is the
    term a version applies from (20232 = autumn 2023) and `eligibility` the
    Särskild behörighet as plain text, "" when KTH states none. `None` when no
    version decodes: a renamed blob must not read as "no requirement"."""
    store = _decode_kth_state_blob(html, _COURSE_PAGE_STATE_RE) or {}
    versions = []
    for syllabus in (store.get("courseData") or {}).get("syllabusList") or []:
        valid_from = syllabus.get("course_valid_from") or {}
        year, semester = valid_from.get("year"), valid_from.get("semesterNumber")
        if isinstance(year, int) and isinstance(semester, int):
            versions.append(
                {
                    "term": year * 10 + semester,
                    "eligibility": _strip_html_to_text(syllabus.get("course_eligibility") or ""),
                }
            )
    versions.sort(key=lambda v: v["term"], reverse=True)
    return versions or None


@dataclass
class WarmReport:
    programme_pages: int = 0
    course_pages: int = 0
    gaps: list[str] = field(default_factory=list)


def _fetch_and_parse(
    cfg: Config, url: str, parse: Callable[[str], list[dict] | None]
) -> list[dict] | None:
    """Runs in a worker thread, so it never touches the cache: SQLite writes
    stay on the calling thread. A page KTH reshaped is unreadable, not fatal."""
    if not _is_allowed_url(url, cfg, _compiled_patterns(cfg)):
        log.warning("study-plan cache: blocked non-allowlisted url %s", url)
        return None
    try:
        _, html = _fetch_html(url, cfg, timeout=cfg.study_plan_cache.fetch_timeout_seconds)
        data = parse(html)
    except Exception as e:
        log.warning("study-plan cache: could not read %s: %s", url, e)
        return None
    if data is None:
        log.warning("study-plan cache: nothing decoded from %s", url)
    return data


def _warm_pages(
    cfg: Config, cache: WebCache, urls: list[str], parse: Callable[[str], list[dict] | None]
) -> tuple[dict[str, list[dict]], list[str]]:
    """Refetch every URL: data per URL, and the URLs that could not be read. An
    unreadable page keeps its older entry, which is also what `data` returns."""
    data: dict[str, list[dict]] = {}
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=cfg.study_plan_cache.max_concurrent_fetches) as pool:
        results = pool.map(lambda u: _fetch_and_parse(cfg, u, parse), urls)
        for url, fresh in zip(urls, results, strict=True):
            if fresh is not None:
                cache.put(
                    CachedPage(
                        url=cache_key(url),
                        title="",
                        content=json.dumps(fresh, ensure_ascii=False),
                        fetched_at=int(time.time()),
                    )
                )
                data[url] = fresh
                continue
            failed.append(url)
            if (old := cache.get(cache_key(url))) is not None:
                data[url] = json.loads(old.content)
    return data, failed


def _warm(cfg: Config) -> WarmReport:
    sc = cfg.study_plan_cache
    report = WarmReport()
    cache = WebCache(cfg)
    latest = latest_cohort(time.localtime())
    cohorts = range(latest - sc.cohorts + 1, latest + 1)

    programme_of: dict[str, str] = {}
    label_of: dict[str, str] = {}
    for code in (c.upper() for c in sc.programmes):
        for cohort in cohorts:
            for year in range(1, sc.years_per_programme + 1):
                url = programme_year_url(code, cohort, year)
                programme_of[url] = code
                label_of[url] = f"{code} HT{cohort} åk{year}"
    year_pages, failed = _warm_pages(cfg, cache, list(programme_of), parse_programme_year)
    report.programme_pages = len(year_pages)
    report.gaps += [label_of[u] for u in failed]
    if failed and len(failed) == len(programme_of):
        # kth.se is down: 500 more timeouts would only add an hour to the run.
        report.gaps.append("no programme page readable: kursplaner not refreshed")
        return report

    course_codes: set[str] = set()
    with_courses: set[str] = set()
    for url, courses in year_pages.items():
        if courses:
            with_courses.add(programme_of[url])
            course_codes.update(c["code"] for c in courses)
    # Readable but empty everywhere: a discontinued or misspelled code. With
    # unreadable pages the gap is already reported above.
    with_failures = {programme_of[u] for u in failed}
    report.gaps += [
        f"{code}: no courses in any cohort"
        for code in dict.fromkeys(programme_of.values())
        if code not in with_courses and code not in with_failures
    ]

    code_of = {COURSE_PAGE_URL.format(code=c): c for c in sorted(course_codes)}
    course_pages, failed = _warm_pages(cfg, cache, list(code_of), parse_course_page)
    report.course_pages = len(course_pages)
    if failed:
        report.gaps.append(
            f"{len(failed)} kursplaner unreadable: {', '.join(code_of[u] for u in failed)}"
        )
    return report


def warm_study_plans(cfg: Config) -> WarmReport:
    """Fetch every configured programme's course lists and the kursplaner they
    name into the cache. Never raises: `maintain.sh` aborts before the reindex
    on a failed scrape, and a KTH outage must not cost the corpus refresh."""
    if not cfg.study_plan_cache.enabled:
        return WarmReport()
    try:
        return _warm(cfg)
    except Exception as e:
        log.exception("study-plan cache: warm aborted")
        return WarmReport(gaps=[f"warm aborted: {e}"])
