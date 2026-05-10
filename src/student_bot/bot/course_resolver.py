"""Course-without-code disambiguation.

When the user asks about a course by name (no [A-Z]{2}[0-9]{4}-style code),
this module finds candidate KTH course codes by fuzzy-matching against a
known program's kurslista (Bilaga 1: Kurslista). A program code is required —
either established this turn (via the program-alias resolver) or carried
over from prior turns via ``program_prior``.

A KTH-wide course-search fallback is intentionally not implemented yet: the
internal endpoint returns ``errorCode: no-restrictions`` without the right
filter parameters, and we'd rather surface no result than a misleading one.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.request import Request, urlopen

from student_bot.config import Config

log = logging.getLogger("student_bot")

# KTH course code pattern, lifted from web_retrieval._COURSE_CODE_RE (kept
# duplicated here to avoid an import cycle; the master copy stays in
# web_retrieval since the regex is used by URL extraction there too).
_COURSE_CODE_RE = re.compile(
    r"\b("
    r"(?!(?:HT|VT)[0-9]{4}\b)[A-Z]{2}[0-9]{4}"
    r"|"
    r"[A-Z]{2}[0-9]{3}[A-Z]"
    r")\b"
)
_STRICT_COURSE_TOKEN = re.compile(
    r"^(?:(?!(?:HT|VT)[0-9]{4}$)[A-Z]{2}[0-9]{4}|[A-Z]{2}[0-9]{3}[A-Z])$"
)

_COURSE_INTENT_KEYWORDS = (
    # Swedish
    "kurs",
    "kursen",
    "kurser",
    "kursboken",
    "kursplan",
    "kursplanen",
    "tenta",
    "tentan",
    "tentamen",
    "tentor",
    "föreläsning",
    "föreläsningen",
    "föreläsningar",
    "labb",
    "labben",
    "labbar",
    "examination",
    # English
    "course",
    "courses",
    "textbook",
    "exam",
    "lecture",
    "lectures",
    "lab",
    "labs",
)

_STOPWORDS_SCORE = {
    "i",
    "om",
    "för",
    "av",
    "på",
    "och",
    "eller",
    "är",
    "den",
    "det",
    "en",
    "ett",
    "in",
    "on",
    "for",
    "of",
    "and",
    "or",
    "the",
    "is",
    "are",
    "to",
}


@dataclass
class CourseHit:
    code: str
    name: str
    score: float
    url: str
    source: str  # "kurslista" today; reserved for future "search"


@dataclass
class CourseResolution:
    course_urls: list[str] = field(default_factory=list)
    clarification_sv: str = ""
    clarification_en: str = ""
    matched_phrase: str = ""


_COURSE_LIST_CACHE: dict[tuple[str, str], tuple[float, list[tuple[str, str]]]] = {}
_COURSE_LIST_CACHE_TTL_SECONDS = 24 * 3600.0


def question_has_course_intent(q: str) -> bool:
    """True when the question mentions a course-intent keyword (kurs / course / tenta / …)."""
    qn = (q or "").lower()
    for kw in _COURSE_INTENT_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", qn):
            return True
    return False


def question_has_explicit_course_code(q: str) -> bool:
    return bool(_COURSE_CODE_RE.search(q or ""))


def _norm(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFC", s or "").lower()
    return re.sub(r"\s+", " ", s).strip()


_COURSE_KEYWORD_RE = re.compile(
    r"\b(?:kurs[a-zåäö]*|tenta[a-zåäö]*|föreläsning[a-zåäö]*|labb[a-zåäö]*|"
    r"examination[a-zåäö]*|course[a-z]*|textbook|exam|lecture[s]?|lab[s]?)\b"
)
_PHRASE_PREP_RE = re.compile(r"\b(?:i|om|för|på|inom|in|on|of|for|about)\s+([^.?!]{3,80})")


def _candidate_phrase(q: str) -> str:
    """Extract the noun phrase that names the course.

    Scans the question for the first ``(i|om|för|on|in|of|for|...) <phrase>``
    after a course-intent keyword. Falls back to the longest such phrase
    anywhere in the question, then to the whole question.
    """
    qn = _norm(q)
    kw_match = _COURSE_KEYWORD_RE.search(qn)
    search_from = kw_match.end() if kw_match else 0
    after_kw = qn[search_from:]

    def _clean(p: str) -> str:
        p = p.strip()
        p = re.sub(r"\s+(?:i|in)\s+[A-Z]{5}\s*$", "", p, flags=re.IGNORECASE)
        p = re.sub(r"\b(?:vid|på|at)\s+kth\s*$", "", p, flags=re.IGNORECASE).strip()
        return p

    m = _PHRASE_PREP_RE.search(after_kw)
    if m:
        phrase = _clean(m.group(1))
        if phrase:
            return phrase

    # Fallback: longest preposition-anchored phrase in the whole question.
    best = ""
    for m in _PHRASE_PREP_RE.finditer(qn):
        cand = _clean(m.group(1))
        if len(cand) > len(best):
            best = cand
    return best or qn


def _fuzzy_score(query: str, candidate: str) -> float:
    """Token-overlap similarity in [0, 1] with a sequence-match fallback."""
    q_tokens = {
        t
        for t in re.findall(r"[a-z0-9åäö]+", _norm(query))
        if len(t) >= 3 and t not in _STOPWORDS_SCORE
    }
    c_tokens = {t for t in re.findall(r"[a-z0-9åäö]+", _norm(candidate)) if len(t) >= 3}
    if not q_tokens or not c_tokens:
        return 0.0
    overlap = q_tokens & c_tokens
    if not overlap:
        return 0.4 * difflib.SequenceMatcher(None, _norm(query), _norm(candidate)).ratio()
    p = len(overlap) / len(q_tokens)
    r = len(overlap) / len(c_tokens)
    return (2 * p * r / (p + r)) if (p + r) else 0.0


def _fetch_html(cfg: Config, url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": cfg.dynamic_web.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(req, timeout=cfg.dynamic_web.timeout_seconds) as resp:
        payload = resp.read(cfg.dynamic_web.max_bytes + 1)
        if len(payload) > cfg.dynamic_web.max_bytes:
            raise ValueError("response exceeded max_bytes")
        return payload.decode("utf-8", errors="replace")


def _walk_courses_in_store(store: dict | None) -> list[tuple[str, str]]:
    """Pull (code, name) pairs out of /studyYearCourses/<scope>/<year>/<bucket>[]."""
    out: list[tuple[str, str]] = []
    if not isinstance(store, dict):
        return out
    sy = store.get("studyYearCourses")
    if not isinstance(sy, dict):
        return out

    def _take(item: object) -> None:
        if not isinstance(item, dict):
            return
        code = item.get("code") or item.get("courseCode")
        name = item.get("name") or ""
        if isinstance(code, str) and isinstance(name, str) and _STRICT_COURSE_TOKEN.fullmatch(code):
            out.append((code, name))

    def _walk(node: object, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            _take(node)
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                _walk(v, depth + 1)

    _walk(sy)
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for code, name in out:
        if code in seen:
            continue
        seen.add(code)
        deduped.append((code, name))
    return deduped


def _fetch_program_courses(cfg: Config, code: str, term: str) -> list[tuple[str, str]]:
    """List courses on a program's kurslista page, cached for 24h per (code, term)."""
    cache_key = (code, term)
    now = time.time()
    cached = _COURSE_LIST_CACHE.get(cache_key)
    if cached and now - cached[0] < _COURSE_LIST_CACHE_TTL_SECONDS:
        return cached[1]
    url = f"https://www.kth.se/student/kurser/program/{code}/{term}/kurslista"
    try:
        html = _fetch_html(cfg, url)
    except Exception as e:
        log.warning("course-resolver: kurslista fetch failed for %s/%s: %s", code, term, e)
        _COURSE_LIST_CACHE[cache_key] = (now, [])
        return []
    # Local import to avoid an import cycle at module load.
    from student_bot.bot.web_retrieval import _compressed_application_store

    store = _compressed_application_store(html)
    pairs = _walk_courses_in_store(store)
    _COURSE_LIST_CACHE[cache_key] = (now, pairs)
    log.info("course-resolver: cached %d courses for kurslista %s/%s", len(pairs), code, term)
    return pairs


def resolve_course_intent(
    cfg: Config,
    question: str,
    *,
    program_prior: str | None = None,
    program_now: str | None = None,
    max_results: int = 5,
    min_score: float = 0.3,
) -> CourseResolution | None:
    """Return a course-resolution clarification (or auto-resolved URL list).

    Triggers only when the question mentions a course-intent keyword, has no
    explicit course code, and a program code is known. Returns ``None``
    otherwise so callers fall through to normal retrieval.
    """
    if not question_has_course_intent(question):
        return None
    if question_has_explicit_course_code(question):
        return None

    code = (program_now or program_prior or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{5}", code):
        return None

    # Local import: web_retrieval imports nothing from this module.
    from student_bot.bot.web_retrieval import _cached_terms_for_code

    terms = _cached_terms_for_code(cfg, code)
    if not terms:
        return None
    term = terms[0]  # most recent intake (terms are sorted desc)

    courses = _fetch_program_courses(cfg, code, term)
    if not courses:
        return None

    phrase = _candidate_phrase(question)
    scored: list[CourseHit] = []
    for c_code, name in courses:
        s = _fuzzy_score(phrase, name)
        if s <= 0:
            continue
        scored.append(
            CourseHit(
                code=c_code,
                name=name,
                score=s,
                url=f"https://www.kth.se/student/kurser/kurs/{c_code}",
                source="kurslista",
            )
        )

    scored.sort(key=lambda h: -h.score)
    top = [h for h in scored if h.score >= min_score][:max_results]
    if not top:
        return None

    log.info(
        "course-resolver: phrase=%r program=%s top=%s",
        phrase,
        code,
        [(h.code, round(h.score, 2), h.name[:40]) for h in top],
    )

    if len(top) == 1:
        return CourseResolution(course_urls=[top[0].url], matched_phrase=phrase)

    sv_lines = [f"- **{h.code}** {h.name}" for h in top]
    en_lines = [f"- **{h.code}** {h.name}" for h in top]
    sv = (
        f"Du nämnde inte någon kurskod. Här är kurser i **{code}** som matchar "
        f'"{phrase}". Vilken menar du? Skicka kurskoden så svarar jag:\n' + "\n".join(sv_lines)
    )
    en = (
        f"You didn't include a course code. Here are courses in **{code}** "
        f'matching "{phrase}". Which one do you mean? Reply with the code:\n' + "\n".join(en_lines)
    )
    return CourseResolution(clarification_sv=sv, clarification_en=en, matched_phrase=phrase)


__all__ = [
    "CourseHit",
    "CourseResolution",
    "question_has_course_intent",
    "question_has_explicit_course_code",
    "resolve_course_intent",
]
