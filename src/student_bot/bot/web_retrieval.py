from __future__ import annotations

import logging
import json
import re
import time
import unicodedata
from typing import Any
from collections import defaultdict
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from student_bot.bot.retrieval import RetrievedChunk
from student_bot.bot.web_cache import CachedPage, WebCache
from student_bot.config import Config

log = logging.getLogger("student_bot")

_KTH_HOST = "www.kth.se"
_KTH_SCHEME = "https"
# KTH course codes: either LL#### (two letters + four digits), or thesis-style
# LL###L (two letters + three digits + letter), e.g. SK110X. Exclude HT/VT + year.
_COURSE_CODE_RE = re.compile(
    r"\b("
    r"(?!(?:HT|VT)[0-9]{4}\b)[A-Z]{2}[0-9]{4}"
    r"|"
    r"[A-Z]{2}[0-9]{3}[A-Z]"
    r")\b"
)
_PROGRAM_CODE_RE = re.compile(r"\b([A-Z]{5})\b")
_PROGRAM_LIST_EN = "https://www.kth.se/student/kurser/kurser-inom-program?l=en"
_PROGRAM_LIST_SV = "https://www.kth.se/student/kurser/kurser-inom-program"
_PROGRAM_URL_CODE_RE = re.compile(r"/student/kurser/program/([A-Z]{5})(?:/|$)")
_KTH_COURSE_PAGE_CODE_RE = re.compile(r"/student/kurser/kurs/([^/]+)", re.I)
_PROGRAM_TERM_RE = re.compile(
    r"^/student/kurser/program/([A-Z]{5})/(\d{5})(?:/(arskurs[1-9]))?/?$",
    re.I,
)
# Match a course code as the whole token (embedded JSON strings, no \\b quirks).
_STRICT_COURSE_TOKEN = re.compile(
    r"^(?:"
    r"(?!(?:HT|VT)[0-9]{4}$)[A-Z]{2}[0-9]{4}"
    r"|"
    r"[A-Z]{2}[0-9]{3}[A-Z]"
    r")$",
    re.I,
)

# Cap extracted programme JSON text — study-plan stores can be large.
_MAX_STORE_WALK_NODES = 50_000
_MAX_STORE_COURSE_LINES = 150
_MAX_DYNAMIC_WEB_CHUNK_CHARS = 36_000
_PROGRAM_SIDEBAR_SLUGS = (
    "mal",
    "omfattning",
    "behorighet",
    "genomforande",
    "kurslista",
    "inriktningar",
)

# Sidebar slugs that contribute *zero unique chunks* once `/omfattning` is in
# the queue. KTH study-plan pages are a SPA: every sidebar URL ships the same
# `__compressedApplicationStore__.studyProgramme` JSON. `/genomforande` is
# byte-identical to `/omfattning`. `/mal`, `/behorighet`, `/kurslista` are not
# routed through the structured chunker, so they only contribute the empty
# legacy-blob fallback (~640 chars of page chrome). Excluded from the default
# bundle *and* from link-discovery to keep prompt tokens bounded.
_PROGRAM_SIDEBAR_SLUGS_REDUNDANT = frozenset({"mal", "behorighet", "genomforande", "kurslista"})

# Human-readable sidebar labels for programme study-plan URLs (path segment after
# /program/<CODE>/<TERM>/...).
_PROGRAM_URL_SECTION_LABELS_SV: dict[str, str] = {
    "mal": "Utbildningens mål",
    "omfattning": "Utbildningens omfattning och innehåll",
    "behorighet": "Behörighet och urval",
    "genomforande": "Utbildningens genomförande",
    "kurslista": "Bilaga 1: Kurslista",
    "inriktningar": "Bilaga 2: Inriktningar",
}


def _program_page_section_label(url: str) -> str:
    """Derive a short section name from /student/kurser/program/... URLs."""
    path = urlsplit(url).path.strip("/")
    if not path:
        return ""
    parts = path.split("/")
    try:
        pidx = parts.index("program")
    except ValueError:
        return ""
    rest = parts[pidx + 1 :]
    if len(rest) < 3:
        return ""
    slug = rest[2].strip().lower()
    if slug.startswith("arskurs"):
        digits = slug[7:]
        if digits.isdigit():
            return f"Årskurs {digits}"
    return _PROGRAM_URL_SECTION_LABELS_SV.get(slug, "")


_GENERIC_ALIAS_TOKENS = {
    "program",
    "programmet",
    "master",
    "masterprogram",
    "masterprogrammet",
    "mastersprogramme",
    "programme",
    "utbildning",
    "utbildningsplan",
    # Both ö- and o-forms: the alias file uses "civilingenjör…" (with ö) but
    # earlier versions of this blocklist only had the ASCII spelling, so the
    # generic prefix wasn't being filtered out of real aliases — which
    # silently inflated `_alias_strong_tokens` denominators and skewed
    # `_alias_score` against the longer (more specific) civilingenjör aliases.
    "civilingenjorsutbildning",
    "civilingenjörsutbildning",
    "civilingenjorsprogram",
    "civilingenjörsprogram",
    "civilingenjor",
    "civilingenjör",
    "kth",
    "the",
    "and",
    "for",
    "of",
    "in",
    "i",
}


_COMP_STORE_RE = re.compile(
    r'window\.__compressedApplicationStore__\s*=\s*"([^"]+)"\s*;',
    re.DOTALL,
)


@dataclass
class AdmissionHints:
    """Cohort hints parsed from the user question (programme round on KTH web)."""

    exact_term: str | None = None  # five-digit KTH programme period, e.g. 20242
    year_prefix: str | None = None  # antagningsår / HT|VT year, e.g. 2024


@dataclass
class ProgrammeRootResolution:
    queue_urls: list[str]
    clarification_sv: str = ""
    clarification_en: str = ""
    # KTH returns 200 + an empty-ish root when the programme code is not in their catalogue.
    missing_program_codes: tuple[str, ...] = ()


def _parse_programme_year_level(q: str) -> int | None:
    """Parse asked study-year level (årskurs) from SV/EN phrasing."""
    if not q:
        return None

    m = re.search(r"\b(?:årskurs|arskurs|år|ar|year)\s*([1-9])\b", q, re.I)
    if m:
        return int(m.group(1))

    ordinal_map = {
        "första året": 1,
        "forsta aret": 1,
        "andra året": 2,
        "andra aret": 2,
        "tredje året": 3,
        "tredje aret": 3,
        "fjärde året": 4,
        "fjarde aret": 4,
        "femte året": 5,
        "femte aret": 5,
        "first year": 1,
        "second year": 2,
        "third year": 3,
        "fourth year": 4,
        "fifth year": 5,
    }
    qn = _norm(q)
    for phrase, level in ordinal_map.items():
        if phrase in qn:
            return level
    return None


def program_study_intent_question(q: str) -> bool:
    lower = (q or "").lower()
    if (
        "program" in lower
        or "utbildningsplan" in lower
        or "study plan" in lower
        or "curriculum" in lower
        or "kurslista" in lower
    ):
        return True
    return _parse_programme_year_level(q or "") is not None


# Patterns whose answers don't actually depend on the user's cohort year — used
# to suppress the "which admission round are you in?" clarification on
# questions like "which masters can a CTFYS student apply to?" or
# "what's the grading scale on CTFYS?" (issue #55). When matched, the bot
# falls through to the newest available term silently. A specific year in
# the question, or `parse_program_admission_hints`, still wins — this guard
# only fires when there's nothing the user has signaled to anchor on.
# Matches both English (`master`, `masters`, `masterprogram`,
# `masterprogrammet`) and Swedish plurals (`mastrar`, `mastrarna`). The KTH
# vernacular borrows the English root with Swedish endings, so we accept the
# whole family.
_MASTER_TOKEN_RE = re.compile(
    r"\bmast(?:er(?:program(?:met|s)?|s)?|rar(?:na)?)\b",
    re.IGNORECASE,
)
_ELIGIBILITY_TOKEN_RE = re.compile(
    r"\b(?:behörig\w*|behorig\w*|krav|krävs|kravs|eligib\w*|requirement\w*|qualify\w*|qualif\w*)\b",
    re.IGNORECASE,
)
_COURSE_LISTING_RE = re.compile(
    r"\b(?:vilka\s+kurser|which\s+courses|lista\s+(?:alla\s+)?kurser|course\s+list|kurslista|ingår\s+i\s+programmet)\b",
    re.IGNORECASE,
)
_GENERIC_METADATA_RE = re.compile(
    r"\b(?:vad\s+heter|what\s+is\s+the\s+program(?:me)?(?:\s+called)?|hur\s+stort|how\s+(?:big|large)|antal\s+poäng|"
    r"examen|betyg(?:sskalan|sskala|en)?|grading|grade\s+scale|study\s+load|credits|hp\b)\b",
    re.IGNORECASE,
)
_MASTER_MAPPING_RE = re.compile(
    r"\b(?:vilka\s+mast(?:er\w*|rar\w*)|which\s+master\w*|mappade?|mapped\s+to|"
    r"which\s+masters?\s+(?:can|may))\b",
    re.IGNORECASE,
)


def _question_is_master_eligibility(q: str) -> bool:
    """True when the question is about which masters a civ-eng student can
    apply to, or which courses qualify for a master. Used to ensure the
    relevant civilingenjör programme's year pages (where the
    `behörighetsgivande kurser per masterprogram` blocks live) get fetched,
    rather than the master programme's own page which doesn't list those
    courses.
    """
    if not q:
        return False
    text = q.strip()
    if _MASTER_MAPPING_RE.search(text):
        return True
    if _MASTER_TOKEN_RE.search(text) and _ELIGIBILITY_TOKEN_RE.search(text):
        return True
    return False


# Heuristic: codes assigned to civilingenjör programmes in the KTH alias
# snapshot all start with `C`, plus a handful of legacy 3-year `T`-prefixed
# högskoleingenjör codes (TIELF, TIMAF, TKEMV). Master programmes start with
# `T` and end in `M`. The alias map is authoritative when available — this
# regex is the cold-cache fallback.
_CIV_CODE_HEURISTIC_RE = re.compile(r"^(?:C[A-Z]{4}|ARKIT|TIELF|TIMAF|TKEMV)$")
_MASTER_CODE_HEURISTIC_RE = re.compile(r"^T[A-Z]{3}M$")


def _is_civilingenjor_code(code: str, cfg: Config | None = None) -> bool:
    """True when `code` is a civilingenjör (or arkitekt) programme code.

    Prefers the alias snapshot — a code's primary Swedish alias starts with
    `civilingenjör` for civ-eng programmes. Falls back to the prefix
    heuristic when the alias cache hasn't been populated yet.
    """
    if not code:
        return False
    upper = code.strip().upper()
    if not re.fullmatch(r"[A-Z]{5}", upper):
        return False
    if cfg is not None:
        try:
            aliases = _get_program_aliases(cfg)
        except Exception:
            aliases = {}
        if aliases:
            for alias, mapped in aliases.items():
                if str(mapped).upper() == upper and "civilingenj" in alias.lower():
                    return True
            for alias, mapped in aliases.items():
                if str(mapped).upper() == upper and (
                    "arkitekt" in alias.lower() or "architecture" in alias.lower()
                ):
                    return True
            return bool(_CIV_CODE_HEURISTIC_RE.match(upper))
    return bool(_CIV_CODE_HEURISTIC_RE.match(upper))


def _question_is_year_independent(q: str) -> bool:
    """True for question shapes whose answers don't vary by admission year.

    Used as an escape hatch around the multi-term clarification branch in
    `_select_programme_urls`: when the question is general (eligibility,
    mapping, programme metadata, course listing without a year qualifier),
    we use the newest term unilaterally rather than re-prompting.
    """
    if not q:
        return False
    text = q.strip()
    if _parse_programme_year_level(text) is not None:
        # The user explicitly named a study year — that hint may or may not
        # imply a cohort, but they've added enough specificity that we should
        # stay on the regular admission-term track.
        return False
    if _MASTER_MAPPING_RE.search(text):
        return True
    if _MASTER_TOKEN_RE.search(text) and _ELIGIBILITY_TOKEN_RE.search(text):
        return True
    if _COURSE_LISTING_RE.search(text):
        return True
    if _GENERIC_METADATA_RE.search(text):
        return True
    return False


def parse_program_admission_hints(q: str) -> AdmissionHints:
    """Prefer explicit five-digit rounds, then HT/VT / Swedish season, then weak context."""
    if not q:
        return AdmissionHints()
    u = q.upper()

    m = re.search(r"\b(20\d{3})\b", u)
    if m:
        return AdmissionHints(exact_term=m.group(1))

    m = re.search(r"\bHT[- ]?\s*(20\d{2})\b", u)
    if m:
        return AdmissionHints(year_prefix=m.group(1))
    m = re.search(r"\bVT[- ]?\s*(20\d{2})\b", u)
    if m:
        return AdmissionHints(year_prefix=m.group(1))

    for pat in (
        r"(?:HÖSTEN|HÖST|HOSTEN|HOST)\s+[-]?\s*(20\d{2})\b",
        r"(?:VÅREN|VÅR|VAREN|VAR)\s+[-]?\s*(20\d{2})\b",
    ):
        m = re.search(pat, q, re.I)
        if m:
            return AdmissionHints(year_prefix=m.group(1))

    # Conversational cohort replies (e.g. after bot asked for admission round).
    if re.search(
        r"\b(?:började|startade|påbörjade|påbörjat|antagen|antagna|intagen)\b",
        q,
        re.I,
    ):
        m = re.search(r"\b(20\d{2})\b", q)
        if m:
            return AdmissionHints(year_prefix=m.group(1))
    if re.search(r"\b(?:started|began)\b", q, re.I):
        m = re.search(r"\b(20\d{2})\b", q)
        if m:
            return AdmissionHints(year_prefix=m.group(1))

    if program_study_intent_question(q):
        m = re.search(
            r"(?:ANTAGE|ANTAGN|INTAG|COHORT|ADMISSION|ADMITTED)[A-Z\s,;:?'\u2019-]*\b(20\d{2})\b",
            u,
        )
        if m:
            return AdmissionHints(year_prefix=m.group(1))
        m = re.search(
            r"\b(?:PROGRAM|PROGRAMMET|UTBILDNINGSPLAN|STUDY\s+PLAN)\b[^?.!\n]{0,200}\b(20\d{2})\b",
            u,
        )
        if m:
            return AdmissionHints(year_prefix=m.group(1))
    return AdmissionHints()


def is_programme_clarification_assistant_message(content: str) -> bool:
    """True if this assistant text is our bilingual admission-round clarification."""
    c = (content or "").lower()
    return (
        "antagningsomgång" in c
        or "vilken antagningsomgång" in c
        or "admission round" in c
        or ("utbildningsplan" in c and "behöver jag veta" in c)
        or ("study plan" in c and "admission" in c and "which" in c)
    )


def is_multi_program_clarification_assistant_message(content: str) -> bool:
    """True if this assistant text is our 'which program code do you mean?' list."""
    c = (content or "").lower()
    return "ditt program är inte entydigt" in c or "your program reference is ambiguous" in c


def _is_clarification_followup_anchor(content: str) -> bool:
    return is_programme_clarification_assistant_message(
        content
    ) or is_multi_program_clarification_assistant_message(content)


def merge_programme_clarification_followup(question: str, history: list[dict] | None) -> str:
    """If the user is answering our admission-year or program-pick question, fuse with the prior user ask."""
    hist = history or []
    if len(hist) < 2:
        return question
    last = hist[-1]
    if last.get("role") != "assistant":
        return question
    last_content = last.get("content", "")
    is_round = is_programme_clarification_assistant_message(last_content)
    is_pick = is_multi_program_clarification_assistant_message(last_content)
    if not (is_round or is_pick):
        return question

    qstrip = question.strip()
    if is_round:
        hints = parse_program_admission_hints(question)
        bare_year = bool(re.fullmatch(r"20\d{2}", qstrip))
        if not (hints.exact_term or hints.year_prefix or bare_year):
            return question
    else:
        # Program pick: require either a 5-letter code or an explicit "I mean X"
        # / "jag menar X" anchor. A bare short message could just be a topic
        # shift, so we don't fuse on length alone.
        has_code = bool(_PROGRAM_CODE_RE.search(qstrip))
        has_pick_anchor = bool(
            re.search(r"\b(?:jag\s+menar|menar)\b", qstrip, re.IGNORECASE)
            or re.search(r"\bi\s+mean\b", qstrip, re.IGNORECASE)
        )
        if not (has_code or has_pick_anchor):
            return question

    prev_user = ""
    for entry in reversed(hist[:-1]):
        if entry.get("role") == "user":
            prev_user = (entry.get("content") or "").strip()
            break
    if not prev_user:
        return question
    merged = f"{prev_user}\n\n{qstrip}"
    log.info(
        "dynamic-web: merged %s clarification follow-up with prior user question",
        "admission-round" if is_round else "program-pick",
    )
    return merged


def history_without_programme_clarification_tail(
    history: list[dict], programme_followup_merged: bool
) -> list[dict]:
    """Drop the last user+assistant pair when folded into the current user prompt."""
    if not programme_followup_merged or len(history) < 2 or history[-1].get("role") != "assistant":
        return history
    if not _is_clarification_followup_anchor(history[-1].get("content", "")):
        return history
    return history[:-2]


@dataclass
class WebFetchResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    used_stale_cache: bool = False
    stale_age_days: int = 0
    failure_url: str = ""
    # Bilingual clarification when cohort (programme period) can't be inferred,
    # or when a colloquial program reference matched several KTH codes.
    clarification: tuple[str, str] | None = None
    # KTH may return 200 + empty SPA shell (h1 «undefined …») for non-existent codes.
    missing_kth_course: tuple[str, str] | None = None
    missing_kth_program: tuple[str, str] | None = None
    # Five-letter KTH code resolved by this fetch, when exactly one program was
    # narrowed to. Surfaced so the pipeline can persist it in conversation memory.
    resolved_program_code: str | None = None
    # Admission round actually used (after falling back to a persisted prior
    # when the current turn carries no hint). Surfaced for the pipeline to
    # persist so a follow-up that doesn't restate the term still routes to
    # the same cohort's study plan.
    applied_admission_term: str | None = None
    applied_admission_year_prefix: str | None = None


def _compiled_patterns(cfg: Config) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in cfg.dynamic_web.allowed_patterns]


def _canonicalize(url: str) -> str:
    """Normalize KTH URLs – host/scheme, collapse path slashes, strip query and fragment."""
    s = urlsplit(url)
    path = re.sub(r"/{2,}", "/", s.path or "/")
    return urlunsplit((_KTH_SCHEME, _KTH_HOST, path.rstrip("/") or "/", "", ""))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _kth_course_code_from_course_url(url: str) -> str | None:
    m = _KTH_COURSE_PAGE_CODE_RE.search(urlsplit(url).path)
    if not m:
        return None
    code = m.group(1).strip().upper()
    return code or None


def _is_kth_placeholder_course_shell(title: str) -> bool:
    """True when KTH returns the empty course SPA (h1 is repeated "undefined")."""
    raw = (title or "").strip()
    if not raw:
        return False
    tokens = re.split(r"\s+", raw.lower())
    return len(tokens) >= 3 and all(tok == "undefined" for tok in tokens)


def _programme_root_title_is_unknown_code_shell(title: str) -> bool:
    """True when the HTML `<title>` is only "CODE (CODE), Utbildningsplaner" (unknown programme).

    KTH serves HTTP 200 for invented codes; the visible "utbildningsplan saknas" text is
    client-rendered, but the document title stays in this stub form server-side.
    """
    if not title:
        return False
    head = title.replace("\xa0", " ").split("|", 1)[0].strip()
    return bool(
        re.fullmatch(r"([A-Za-z0-9]{3,10})\s*\(\1\)\s*,\s*Utbildningsplaner", head, flags=re.I)
    )


def _bilingual_missing_kth_course_message(codes: list[str]) -> tuple[str, str]:
    uniq = list(dict.fromkeys(codes))
    tail = uniq[0] if len(uniq) == 1 else ", ".join(uniq)
    return (
        f"KTH:s kurssidor listar ingen kurs med koden {tail} – sidan är bara en tom "
        "mall, så kurskoden finns troligen inte. Kontrollera stavningen på kth.se eller "
        "antagning.se. Vid behov, kontakta studievägledningen.",
        f"KTH's course pages do not list code(s) {tail} — the response is only an empty "
        "template, so the code likely does not exist. Double-check spelling on kth.se or "
        "antagning.se; contact study counseling if needed.",
    )


def _bilingual_missing_kth_program_message(codes: list[str]) -> tuple[str, str]:
    uniq = list(dict.fromkeys(codes))
    tail = uniq[0] if len(uniq) == 1 else ", ".join(uniq)
    return (
        f"KTH:s programkatalog listar ingen utbildning med koden {tail} – sidan är bara "
        "en tom stub (inga antagningsomgångar i KTH:s data). Kontrollera koden på "
        "https://www.kth.se/student/kurser/kurser-inom-program eller antagning.se.",
        f"KTH's programme catalogue has no programme with code {tail} — the page is only "
        "an empty stub (no admission rounds in KTH's data). Verify the code on "
        "https://www.kth.se/student/kurser/kurser-inom-program?l=en or universityadmissions.se.",
    )


def _is_allowed_url(url: str, cfg: Config, patterns: list[re.Pattern[str]]) -> bool:
    s = urlsplit(url)
    if s.scheme != _KTH_SCHEME or s.netloc != _KTH_HOST:
        return False
    path = s.path.rstrip("/") or "/"
    with_slash = path if path.endswith("/") else f"{path}/"
    return any(p.fullmatch(path) or p.fullmatch(with_slash) for p in patterns)


def _parse_program_aliases_from_html(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    aliases: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = re.search(r"/student/kurser/program/([A-Z]{5})(?:/|$)", href)
        if not m:
            continue
        code = m.group(1)
        label = _norm(a.get_text(" ", strip=True))
        if label:
            aliases[label] = code
            # Also keep a stripped version without trailing "(CODE)".
            stripped = _norm(re.sub(r"\s*\([A-Z]{5}\)\s*$", "", label, flags=re.I))
            if stripped:
                aliases[stripped] = code
        aliases[code.lower()] = code
    # Fallback: KTH may render the programme catalogue only via embedded JSON.
    if not aliases:
        aliases.update(_parse_program_aliases_from_store(html))
    return aliases


def _parse_program_aliases_from_store(html: str) -> dict[str, str]:
    """Extract programme (study-plan) codes from __compressedApplicationStore__.

    KTH's /student/kurser/kurser-inom-program page is a SPA; the server-side HTML
    may not contain `<a href="/student/kurser/program/...">` links anymore.
    """
    store = _compressed_application_store(html or "")
    if not isinstance(store, dict):
        return {}
    programmes = store.get("programmes")
    if not isinstance(programmes, list):
        return {}

    out: dict[str, str] = {}

    def maybe_add(code: str | None, title: str | None) -> None:
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{5}", code):
            return
        if not isinstance(title, str) or not title.strip():
            return
        label = _norm(title)
        if not label:
            return
        out[label] = code
        stripped = _norm(re.sub(r"\s*\([A-Z]{5}\)\s*$", "", title, flags=re.I))
        if stripped:
            out[stripped] = code
        out[code.lower()] = code

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            code = o.get("programmeCode") or o.get("code")
            title = (
                o.get("title")
                or o.get("titleSv")
                or o.get("title_sv")
                or o.get("name")
                or o.get("nameSv")
                or o.get("name_sv")
            )
            maybe_add(str(code).strip().upper() if isinstance(code, str) else None, title)
            # Also index other-language title when available (helps EN queries).
            title_en = o.get("titleOtherLanguage") or o.get("titleEn") or o.get("title_en")
            maybe_add(str(code).strip().upper() if isinstance(code, str) else None, title_en)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(programmes)
    return out


def _fetch_program_aliases_page(url: str, cfg: Config) -> dict[str, str]:
    log.info("dynamic-web aliases: fetching %s", url)
    req = Request(
        url,
        headers={
            "User-Agent": cfg.dynamic_web.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(req, timeout=cfg.dynamic_web.timeout_seconds) as resp:
        html = resp.read(cfg.dynamic_web.max_bytes + 1).decode("utf-8", errors="replace")
    return _parse_program_aliases_from_html(html)


def _read_alias_cache(path: Path, ttl_hours: int) -> dict[str, str] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = int(data.get("fetched_at", 0))
        if int(time.time()) - fetched_at > max(1, ttl_hours) * 3600:
            return None
        aliases = data.get("aliases", {})
        return {str(k): str(v) for k, v in aliases.items()}
    except Exception:
        return None


def _write_alias_cache(path: Path, aliases: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"fetched_at": int(time.time()), "aliases": aliases}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_program_aliases(cfg: Config) -> dict[str, str]:
    cache_path = cfg.absolute(Path(cfg.dynamic_web.program_aliases_file))
    cached = _read_alias_cache(cache_path, cfg.dynamic_web.program_aliases_ttl_hours)
    if cached is not None:
        log.info("dynamic-web aliases: using cached aliases from %s", cache_path)
        aliases = dict(cached)
    else:
        aliases = {}
        for url in (_PROGRAM_LIST_EN, _PROGRAM_LIST_SV):
            try:
                aliases.update(_fetch_program_aliases_page(url, cfg))
            except Exception as e:
                log.warning("program alias fetch failed for %s: %s", url, e)
        if aliases:
            _write_alias_cache(cache_path, aliases)
            log.info(
                "dynamic-web aliases: wrote %d aliases to %s",
                len(aliases),
                cache_path,
            )
    # Manual overrides win.
    for k, v in cfg.dynamic_web.program_aliases.items():
        aliases[_norm(k)] = v.upper()
    return aliases


def _alias_strong_tokens(alias: str) -> set[str]:
    """Discriminative tokens of an alias: length ≥ 4, not in the generic block-list."""
    tokens = set(re.findall(r"[a-z0-9åäö]+", alias))
    return {t for t in tokens if len(t) >= 4 and t not in _GENERIC_ALIAS_TOKENS}


def _alias_token_frequency(aliases: dict[str, str]) -> dict[str, int]:
    """How many distinct aliases each strong token appears in.

    Used by the historical-program rescue logic to require at least one
    *rare* matched token before keeping a discontinued candidate in
    disambiguation. A common subject token like `matematik` (present in many
    aliases) shouldn't be enough on its own to rescue an extinct master from
    the recency drop — but a rare token like `fusionsenergi` (1 alias)
    should still rescue TFEPM when the user types it explicitly.
    """
    freq: dict[str, int] = {}
    for alias in aliases:
        for t in _alias_strong_tokens(alias):
            freq[t] = freq.get(t, 0) + 1
    return freq


def _alias_score(
    alias: str, qn: str, q_tokens: set[str], q_strong_tokens: set[str]
) -> tuple[float, bool]:
    """Score a normalised alias against a normalised question.

    Combines two coverages so an alias has to match a meaningful fraction of
    *both* sides — the alias's strong tokens AND the user's strong tokens —
    before it scores well. Without the query-side factor, a one-token alias
    like "masterprogram, matematik" gets coverage=1.0 from any query that
    mentions "matematik", overshadowing the more specific match for queries
    like "teknisk matematik" → "civilingenjörsutbildning i teknisk matematik".

    Score = (alias_coverage * query_coverage) + 0.5 if the full alias phrase
    appears verbatim in the question.

    Returns (score, verbatim_phrase_present). Score 0 = no match.
    """
    if not alias:
        return 0.0, False
    strong = _alias_strong_tokens(alias)
    if not strong:
        # Aliases that are entirely generic (e.g. the lowercased code "ctfys")
        # only count when the question contains the alias verbatim.
        return (1.5, True) if alias and alias == qn else (0.0, False)
    inter = strong & q_tokens
    if not inter:
        return 0.0, False
    alias_coverage = len(inter) / len(strong)
    # Query-side coverage. If the user typed no strong tokens at all (very
    # short query like "ctfys" with only the code), default to 1.0 so we
    # don't penalise short verbatim-code queries that route through here.
    query_coverage = len(inter) / len(q_strong_tokens) if q_strong_tokens else 1.0
    verbatim = alias in qn
    return alias_coverage * query_coverage + (0.5 if verbatim else 0.0), verbatim


def _program_level(code: str) -> str:
    """KTH program codes encode level via the leading letters and final letter."""
    if not code or len(code) != 5:
        return "other"
    if code[0] == "C":
        return "civilingenjor"
    if code.startswith("TI"):
        return "hogskoleingenjor"
    if code[0] == "T" and code[4] == "M":
        return "master"
    if code[0] == "T":
        return "bachelor"
    return "other"


def _level_prior_from_question(question: str) -> set[str] | None:
    """Infer which program levels the user is asking about. None = no signal."""
    qn = _norm(question)
    year = _parse_programme_year_level(question)
    has_civ = "civilingenjör" in qn or "civilingenjor" in qn or "civil engineering" in qn
    has_master = (
        "masterprogram" in qn
        or "master's programme" in qn
        or "master programme" in qn
        or "master's program" in qn
        or "master program" in qn
    )
    has_hogskole = "högskoleingenjör" in qn or "hogskoleingenjor" in qn
    has_bachelor = "kandidatprogram" in qn or "bachelor" in qn

    if year is not None and year <= 3:
        if has_civ:
            return {"civilingenjor"}
        if has_hogskole:
            return {"hogskoleingenjor"}
        if has_bachelor:
            return {"bachelor"}
        # Year ≤ 3 + master keyword: the student is in the civilingenjör phase
        # asking about future master options. Anchor to the current program.
        if has_master:
            return {"civilingenjor"}
        return {"civilingenjor", "hogskoleingenjor", "bachelor"}
    if year is not None and year >= 4:
        if has_civ:
            return {"civilingenjor"}
        if has_master:
            return {"master"}
        return {"civilingenjor", "master"}
    if has_civ and not has_master:
        return {"civilingenjor"}
    if has_master and not has_civ:
        return {"master"}
    if has_hogskole:
        return {"hogskoleingenjor"}
    if has_bachelor:
        return {"bachelor"}
    return None


_program_nicknames_cache: dict[str, tuple[float, dict[str, list[str]]]] = {}
_PROGRAM_NICKNAMES_TTL_SECONDS = 300.0  # mtime check cadence


def _load_program_nicknames(cfg: Config) -> dict[str, list[str]]:
    """Load curated colloquial-name -> [program codes] map. Empty on missing file."""
    path = cfg.absolute(Path(cfg.dynamic_web.program_nicknames_file))
    key = str(path)
    now = time.time()
    cached = _program_nicknames_cache.get(key)
    if cached and now - cached[0] < _PROGRAM_NICKNAMES_TTL_SECONDS:
        return cached[1]
    out: dict[str, list[str]] = {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        _program_nicknames_cache[key] = (now, out)
        return out
    except (OSError, json.JSONDecodeError) as e:
        log.warning("program nicknames load failed for %s: %s", path, e)
        _program_nicknames_cache[key] = (now, out)
        return out
    entries = data.get("entries", {}) if isinstance(data, dict) else {}
    if not isinstance(entries, dict):
        _program_nicknames_cache[key] = (now, out)
        return out
    for key_phrase, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        cands = entry.get("candidates", [])
        if not isinstance(cands, list):
            continue
        codes = [c for c in (str(x).strip().upper() for x in cands) if re.fullmatch(r"[A-Z]{5}", c)]
        if codes:
            out[_norm(str(key_phrase))] = codes
    _program_nicknames_cache[key] = (now, out)
    return out


@dataclass
class _ProgramCandidate:
    code: str
    score: float
    matched_alias: str
    verbatim: bool  # True when the user typed the 5-letter code or the alias verbatim


def _extract_program_candidates(
    question: str, cfg: Config, *, program_prior: str | None = None
) -> tuple[list[_ProgramCandidate], list[str]]:
    """Score program candidates from the question. Returns (candidates, verbatim_codes).

    Candidates are deduped per code (best-scoring alias kept) and narrowed by
    a level prior (civilingenjör / master / etc.) when the question has a clear
    level signal. Verbatim-typed codes always survive narrowing.
    """
    aliases = _get_program_aliases(cfg)
    known_codes = {
        v
        for v in aliases.values()
        if re.fullmatch(r"[A-Z]{5}", v.upper() if isinstance(v, str) else "")
    }
    qn = _norm(question)
    q_tokens = set(re.findall(r"[a-z0-9åäö]+", qn))
    q_strong_tokens = {t for t in q_tokens if len(t) >= 4 and t not in _GENERIC_ALIAS_TOKENS}

    verbatim_codes: list[str] = []
    for code in _PROGRAM_CODE_RE.findall(question):
        if known_codes and code not in known_codes:
            continue
        if code not in verbatim_codes:
            verbatim_codes.append(code)

    by_code: dict[str, _ProgramCandidate] = {}
    for code in verbatim_codes:
        by_code[code] = _ProgramCandidate(code=code, score=2.0, matched_alias=code, verbatim=True)

    min_score = cfg.dynamic_web.alias_min_score
    for alias, code in aliases.items():
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{5}", code):
            continue
        # Skip aliases that are themselves the lowercased 5-letter code; the
        # verbatim-codes pass above handles those.
        if alias and alias.upper() == code:
            continue
        score, verbatim = _alias_score(alias, qn, q_tokens, q_strong_tokens)
        if score < min_score:
            continue
        prev = by_code.get(code)
        if prev is None or score > prev.score:
            by_code[code] = _ProgramCandidate(
                code=code,
                score=score,
                matched_alias=alias,
                verbatim=verbatim or (prev.verbatim if prev else False),
            )
        elif verbatim and not prev.verbatim:
            prev.verbatim = True

    # Curated colloquial-name override. If a phrase from
    # `data/program_nicknames.json` appears in the query, treat its candidate
    # codes as authoritative: keep verbatim-typed codes, keep the nickname
    # codes themselves (boosting them if they were below threshold), and drop
    # alias-only matches that aren't in the curated set. Lets the operator
    # pin a high-traffic colloquial phrase like "teknisk matematik" to CTMAT
    # without having to re-derive it via alias scoring.
    nicknames = _load_program_nicknames(cfg)
    nickname_codes: list[str] = []
    matched_phrase = ""
    for phrase, codes in nicknames.items():
        if phrase and phrase in qn:
            nickname_codes = [c for c in codes if c not in nickname_codes]
            matched_phrase = phrase
            break
    if nickname_codes:
        verbatim_set = set(verbatim_codes)
        kept: dict[str, _ProgramCandidate] = {}
        for code in nickname_codes:
            existing = by_code.get(code)
            if existing is not None:
                kept[code] = existing
            else:
                kept[code] = _ProgramCandidate(
                    code=code,
                    score=0.8,
                    matched_alias=f"<nickname:{matched_phrase}>",
                    verbatim=False,
                )
        for code, cand in by_code.items():
            if code in verbatim_set:
                kept.setdefault(code, cand)
        by_code = kept

    if not by_code and program_prior and re.fullmatch(r"[A-Z]{5}", program_prior):
        # Conversation prior: use the last resolved program when the current
        # turn produced no candidates. Score below alias matches so a fresh
        # signal in this turn would always win.
        by_code[program_prior] = _ProgramCandidate(
            code=program_prior,
            score=0.7,
            matched_alias="<prior>",
            verbatim=False,
        )

    candidates = list(by_code.values())

    # If the user typed any 5-letter code verbatim, it is the unambiguous
    # signal — drop alias-only matches so they don't pull the resolver into
    # the multi-candidate path. (Multiple verbatim codes still produce a
    # multi-candidate clarification.)
    if verbatim_codes:
        verbatim_set = set(verbatim_codes)
        candidates = [c for c in candidates if c.code in verbatim_set]

    levels = _level_prior_from_question(question)
    if levels and len(candidates) > 1:
        narrowed = [c for c in candidates if _program_level(c.code) in levels]
        if narrowed:
            verbatim_extras = [
                c for c in candidates if c.verbatim and c.code not in {n.code for n in narrowed}
            ]
            candidates = narrowed + verbatim_extras

    # Recency penalty: programmes whose most recent intake is older than
    # `historical_program_years` (default 8) get their score halved and may
    # fall below the alias threshold. Skipped for verbatim-typed codes (the
    # user explicitly named it) and for nickname-listed codes (the operator
    # explicitly chose to keep them in the candidate pool). Uses the cached
    # term list (6h TTL) — only fires for candidates that already survived
    # alias scoring, so it doesn't slow down the typical single-candidate
    # path. Last-resort safety: any exception keeps the candidate in.
    if len(candidates) > 1:
        cutoff_year = _current_calendar_year() - cfg.dynamic_web.historical_program_years
        nickname_set = set(nickname_codes)
        kept_after_recency: list[_ProgramCandidate] = []
        for cand in candidates:
            if cand.verbatim or cand.code in nickname_set:
                kept_after_recency.append(cand)
                continue
            try:
                terms = _cached_terms_for_code(cfg, cand.code)
            except Exception:
                kept_after_recency.append(cand)
                continue
            bounds = _intake_year_bounds_from_terms(terms)
            if bounds is None:
                kept_after_recency.append(cand)
                continue
            if bounds[1] < cutoff_year:
                cand.score *= 0.5
                if cand.score < min_score:
                    log.info(
                        "dynamic-web: dropping %s after recency penalty "
                        "(last intake %d < cutoff %d, score %.2f < min %.2f)",
                        cand.code,
                        bounds[1],
                        cutoff_year,
                        cand.score,
                        min_score,
                    )
                    continue
            kept_after_recency.append(cand)
        candidates = kept_after_recency

    candidates.sort(key=lambda c: (-c.score, c.code))
    return candidates, verbatim_codes


def _extract_targets(question: str) -> list[str]:
    raise RuntimeError("_extract_targets requires cfg; use _extract_targets_with_cfg")


def _extract_targets_with_cfg(
    question: str, cfg: Config, *, program_prior: str | None = None
) -> list[str]:
    urls: list[str] = []
    for code in _COURSE_CODE_RE.findall(question.upper()):
        urls.append(f"https://{_KTH_HOST}/student/kurser/kurs/{code}")

    # Program lookup is triggered by explicit program intent words OR explicit
    # study-year phrasing (e.g. "år 2", "second year").
    lower = question.lower()
    program_intent = (
        "program" in lower
        or "utbildningsplan" in lower
        or "study plan" in lower
        or "curriculum" in lower
        or "kurslista" in lower
        or _parse_programme_year_level(question) is not None
        or bool(_PROGRAM_CODE_RE.search(question))
    )

    if program_intent:
        aliases = _get_program_aliases(cfg)
        candidates, verbatim_codes = _extract_program_candidates(
            question, cfg, program_prior=program_prior
        )
        # Cold-cache fallback: when the alias snapshot is empty AND no
        # candidate scored, still probe verbatim-typed codes via the KTH root.
        if not aliases and not candidates:
            for code in _PROGRAM_CODE_RE.findall(question):
                log.warning(
                    "dynamic-web: programme code list empty; "
                    "probing bare token %s directly via KTH root page",
                    code,
                )
                urls.append(f"https://{_KTH_HOST}/student/kurser/program/{code}")
        for cand in candidates:
            urls.append(f"https://{_KTH_HOST}/student/kurser/program/{cand.code}")
        if candidates:
            log.info(
                "dynamic-web: program candidates=%s",
                [(c.code, round(c.score, 2), c.matched_alias) for c in candidates],
            )
        elif verbatim_codes:
            log.info("dynamic-web: verbatim-only program codes=%s", verbatim_codes)

    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        c = _canonicalize(u)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _fetch_html(url: str, cfg: Config) -> tuple[str, str]:
    req = Request(
        url,
        headers={
            "User-Agent": cfg.dynamic_web.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(req, timeout=cfg.dynamic_web.timeout_seconds) as resp:
        final_url = _canonicalize(resp.geturl())
        payload = resp.read(cfg.dynamic_web.max_bytes + 1)
        if len(payload) > cfg.dynamic_web.max_bytes:
            raise ValueError("response exceeded max_bytes")
        html = payload.decode("utf-8", errors="replace")
    return final_url, html


def _sanitize_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "form", "header", "footer", "nav"]):
        t.decompose()

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()

    lines: list[str] = []
    selectors = ["h1", "h2", "h3", "p", "li", "dt", "dd"]
    for node in soup.select(",".join(selectors)):
        txt = unescape(node.get_text(" ", strip=True))
        if not txt:
            continue
        if len(txt) > 900:
            txt = txt[:900] + "…"
        if node.name in ("h1", "h2", "h3"):
            lines.append(f"{'#' * int(node.name[1])} {txt}")
        else:
            lines.append(txt)
    # Course tables sometimes carry almost all visible structure on programme pages.
    for table in soup.find_all("table"):
        rows: list[str] = []
        for tr in table.find_all("tr"):
            cells = [
                unescape(c.get_text(" ", strip=True))
                for c in tr.find_all(["th", "td"])
                if c.get_text(strip=True)
            ]
            if cells:
                row = " | ".join(cells)
                if len(row) > 800:
                    row = row[:800] + "…"
                rows.append(row)
        if rows:
            lines.append("\n".join(rows))
    return title or "KTH page", "\n".join(lines)


def _truncate_web_chunk_text(text: str) -> str:
    if len(text) <= _MAX_DYNAMIC_WEB_CHUNK_CHARS:
        return text
    return text[:_MAX_DYNAMIC_WEB_CHUNK_CHARS].rstrip() + "\n…"


# KTH utbildningsplan: course.Valvillkor / electiveCondition codes.
# VV = "valbara kurslistor" (often called villkorligt valbara/valfria; ~conditionally elective).
_VALVILLKOR_LABEL_SV: dict[str, str] = {
    "O": "Obligatoriska kurser (O)",
    "V": "Valfria kurser (V)",
    "VV": "Valbara kurslistor – villkorligt valbara (VV)",
    "K": "Konditionsvalfria kurser (K)",
    "KV": "Konditionsvalfria kurser (KV)",
    "VK": "Valbara kurslistor – villkorligt valbara (VK)",
}
_VALVILLKOR_SORT_ORDER: tuple[str, ...] = ("O", "K", "KV", "VV", "VK", "V")


def _program_page_year_from_url(url: str) -> int | None:
    """Parse /arskursN from a programme page URL, if present."""
    path = urlsplit(url).path
    m = re.search(r"/arskurs([1-9])(?:/|$)", path, re.I)
    return int(m.group(1)) if m else None


def _sort_valvillkor_keys(keys: list[str]) -> list[str]:
    def sort_key(k: str) -> tuple[int, str]:
        try:
            return (_VALVILLKOR_SORT_ORDER.index(k), k)
        except ValueError:
            return (len(_VALVILLKOR_SORT_ORDER), k)

    return sorted(set(keys), key=sort_key)


def _format_hp_sv_number(n: float) -> str:
    """Swedish-style hp string, e.g. 4 -> "4,0 hp", 7.5 -> "7,5 hp"."""
    return f"{float(n):.1f}".replace(".", ",") + " hp"


def _credits_suffix_sv(c: dict) -> str:
    """Return " (7,5 hp)" from omfattning / credits fields, or ``\"\"`` if unknown."""
    blocks: list[dict] = [c]
    inner = c.get("course")
    if isinstance(inner, dict):
        blocks.append(inner)
    for block in blocks:
        o = block.get("omfattning")
        if isinstance(o, dict):
            fu = o.get("formattedWithUnit")
            if isinstance(fu, str) and fu.strip():
                return f" ({fu.strip()})"
            num_o = o.get("number")
            if isinstance(num_o, (int, float)) and float(num_o) > 0:
                return f" ({_format_hp_sv_number(float(num_o))})"
        fc = block.get("formattedCredits")
        if isinstance(fc, str) and fc.strip():
            return f" ({fc.strip()})"
        cr = block.get("credits")
        if isinstance(cr, (int, float)) and float(cr) > 0:
            return f" ({_format_hp_sv_number(float(cr))})"
    return ""


def _markdown_course_line_from_curriculum_row(c: dict) -> str | None:
    """One bullet line from a curriculums[].studyYears[].courses[] row."""
    raw = c.get("kod") or c.get("courseCode") or c.get("code")
    if not isinstance(raw, str):
        return None
    cc = raw.strip().upper()
    if not _STRICT_COURSE_TOKEN.fullmatch(cc):
        return None
    name = (
        c.get("benamning")
        or c.get("titleSv")
        or c.get("title_sv")
        or c.get("title")
        or c.get("shortTitleSv")
        or c.get("nameSv")
        or c.get("name")
    )
    name_s = name.strip() if isinstance(name, str) else ""
    hp = _credits_suffix_sv(c)
    if name_s:
        return f"- **{cc}** – {name_s}{hp}"
    return f"- **{cc}**{hp}"


def _course_list_flat_fallback_from_store(store: dict) -> str:
    """Legacy walk: any course-shaped dicts anywhere in the JSON tree."""
    seen: set[str] = set()
    lines: list[str] = []
    nodes = 0

    def walk(o: Any) -> None:
        nonlocal nodes
        if len(lines) >= _MAX_STORE_COURSE_LINES or nodes >= _MAX_STORE_WALK_NODES:
            return
        nodes += 1
        if isinstance(o, dict):
            raw_code = (
                o.get("courseCode") or o.get("code") or o.get("course_code") or o.get("courseId")
            )
            if isinstance(raw_code, str):
                cc = raw_code.strip().upper()
                if _STRICT_COURSE_TOKEN.fullmatch(cc) and cc not in seen:
                    name = (
                        o.get("titleSv")
                        or o.get("title_sv")
                        or o.get("title")
                        or o.get("shortTitleSv")
                        or o.get("nameSv")
                        or o.get("name")
                        or o.get("benamning")
                    )
                    name_s = name.strip() if isinstance(name, str) else ""
                    hp = _credits_suffix_sv(o)
                    lines.append(f"- **{cc}** – {name_s}{hp}" if name_s else f"- **{cc}**{hp}")
                    seen.add(cc)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(store)

    if not lines:
        try:
            blob = json.dumps(store, ensure_ascii=False)
        except Exception:
            return ""
        codes = sorted(set(_COURSE_CODE_RE.findall(blob.upper())))
        if not codes:
            return ""
        lines = [f"- `{c}`" for c in codes[:_MAX_STORE_COURSE_LINES]]

    header = (
        "## Kurser (extraherade från KTH:s inbäddade programdata)\n"
        "_Raderna kommer från sidans JavaScript/JSON, inte bara synlig HTML._\n"
    )
    return header + "\n".join(lines)


def _course_list_plaintext_from_store(store: dict | None, *, focus_year: int | None = None) -> str:
    """Course rows from KTH's __compressedApplicationStore__ (SPA programme pages).

    When the store follows the standard ``curriculums[0].studyYears`` shape, keep
    **Obligatoriska / valbara kurslistor (VV) / valfria** (Valvillkor) headings so
    the model can distinguish compulsory, conditionally elective pools, and free
    electives. Otherwise fall back to a flat walk.
    """
    if not store:
        return ""

    lines: list[str] = []
    line_budget = _MAX_STORE_COURSE_LINES
    curriculums = store.get("curriculums")
    if isinstance(curriculums, list) and curriculums:
        cy0 = curriculums[0]
        if isinstance(cy0, dict):
            study_years = cy0.get("studyYears") or []
            if isinstance(study_years, list) and study_years:
                lines.append("## Kurser (KTH utbildningsplan)")
                lines.append(
                    "_Koder i utbildningsplanen (fältet Valvillkor): "
                    "**O** = obligatoriska kurser; "
                    "**V** = valfria kurser; "
                    "**VV** = valbara kurslistor (villkorligt valbara / villkorligt valfria; "
                    "eng. ungefär “conditionally elective” – val inom godkända listor enligt planen). "
                    "Andra koder (t.ex. K) beskrivs i respektive rubrik._"
                )
                years_sorted = sorted(
                    [y for y in study_years if isinstance(y, dict)],
                    key=lambda y: int(y.get("yearNumber") or 0),
                )
                truncated = False
                for y in years_sorted:
                    yn = y.get("yearNumber")
                    if not isinstance(yn, int):
                        continue
                    if focus_year is not None and yn != focus_year:
                        continue
                    lines.append(f"### Årskurs {yn}")
                    for ft in y.get("freeTexts") or []:
                        if isinstance(ft, dict):
                            tx = ft.get("Text")
                            if isinstance(tx, str) and tx.strip():
                                lines.append(f"_Notis:_ {tx.strip()}")
                    buckets: dict[str, list[dict]] = defaultdict(list)
                    for c in y.get("courses") or []:
                        if not isinstance(c, dict):
                            continue
                        raw_vv = c.get("Valvillkor") or c.get("valvillkor") or "?"
                        vv = raw_vv.strip() if isinstance(raw_vv, str) else "?"
                        if not vv:
                            vv = "?"
                        buckets[vv].append(c)
                    for vv in _sort_valvillkor_keys(list(buckets.keys())):
                        label = _VALVILLKOR_LABEL_SV.get(vv, f"Kurser (valvillkor {vv})")
                        lines.append(f"#### {label}")
                        for c in sorted(buckets[vv], key=lambda x: str(x.get("kod") or "")):
                            if line_budget <= 0:
                                truncated = True
                                break
                            row = _markdown_course_line_from_curriculum_row(c)
                            if not row:
                                continue
                            lines.append(row)
                            line_budget -= 1
                        if line_budget <= 0:
                            truncated = True
                            break
                    if line_budget <= 0:
                        break
                if len(lines) > 2:
                    if truncated:
                        lines.append("\n_… avkortad: max antal kursrader._")
                    return "\n".join(lines).strip()

    return _course_list_flat_fallback_from_store(store)


def _programme_page_text_with_store(html: str, visible_body: str, page_url: str = "") -> str:
    """Append course lines from __compressedApplicationStore__ when DOM text is thin."""
    store = _compressed_application_store(html)
    focus_year = _program_page_year_from_url(page_url) if page_url else None
    appendix = _course_list_plaintext_from_store(store, focus_year=focus_year)
    if not appendix:
        return visible_body
    base = visible_body.strip()
    if not base:
        return appendix.strip()
    return f"{base}\n\n{appendix}".strip()


# ---------------------------------------------------------------------------
# Structured per-section chunking for KTH programme pages.
#
# The legacy single-chunk-per-page approach (capped at _MAX_DYNAMIC_WEB_CHUNK_CHARS)
# meant year-1 content dominated the LLM prompt budget; later year info and
# the studyProgramme narrative often got crowded out. Instead we walk the
# SPA store and emit one chunk per logical section, labelled via the curated
# atlas (data/study_plan_atlas.yaml) so retrieval can rank on user intent.
# ---------------------------------------------------------------------------

_MAX_STUDYPLAN_CHUNK_CHARS = 6_000  # per-field cap; avoids one giant blob
_MAX_STUDYPLAN_CHUNKS_PER_PAGE = 30  # belt-and-braces against pathological pages

_VALVILLKOR_BUCKET_LABELS_SV: dict[str, str] = {
    "O": "Obligatoriska",
    "V": "Valbara",
    "VV": "Villkorligt valbara",
    "K": "Kompletterande",
    "KV": "Konditionsvalfria",
    "VK": "Villkorligt valbara",
    "R": "Rekommenderade",
}


def _strip_html_to_text(value: str) -> str:
    """Cheap HTML-to-text – programme freetext fields are short and well-formed."""
    if not value:
        return ""
    soup = BeautifulSoup(value, "lxml")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _flatten_text(value: object) -> str:
    """Best-effort flatten of a studyProgramme field into plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _strip_html_to_text(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                t = _strip_html_to_text(item)
                if t:
                    parts.append(t)
            elif isinstance(item, dict):
                # specialisations / similar: pick code + label + description.
                kod = item.get("kod") or item.get("code") or ""
                ben = item.get("benamning") or item.get("name") or item.get("title") or ""
                besk = item.get("beskrivning") or item.get("description") or ""
                head = " ".join(b for b in [str(kod), str(ben)] if b).strip()
                body = _strip_html_to_text(str(besk)) if besk else ""
                if head and body:
                    parts.append(f"{head}: {body}")
                elif head:
                    parts.append(head)
                elif body:
                    parts.append(body)
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        # Rare — flatten nested string values.
        return "\n".join(
            _strip_html_to_text(v) for v in value.values() if isinstance(v, str) and v.strip()
        )
    return ""


def _truncate_studyplan_text(text: str) -> str:
    if len(text) <= _MAX_STUDYPLAN_CHUNK_CHARS:
        return text
    return text[:_MAX_STUDYPLAN_CHUNK_CHARS].rstrip() + "\n…"


_PROGRAM_BUNDLE_BASE_RE = re.compile(
    r"^(/student/kurser/program/[A-Z]{5}/\d{5})(?:/[a-z][a-z0-9-]*)?/?$",
    re.I,
)


_KULL_TITLE_PROGRAMME_RE = re.compile(r"^(.+?)(?:\s+studieplan:|\s+årskurs\s+\d+|$)", re.IGNORECASE)


def _kull_label_sv(term: str) -> str:
    """KTH 5-digit programme term -> short cohort label, e.g. '20232' -> 'HT23'.

    Matches the convention students use ("kull HT23"), distinct from the
    long form `_term_label_sv` returns ("HT2023") which is used elsewhere.
    """
    if not (isinstance(term, str) and re.fullmatch(r"\d{5}", term)):
        return ""
    yy = term[2:4]
    season = "HT" if term[4] == "2" else "VT"
    return f"{season}{yy}"


def _study_plan_title_with_kull(doc_title: str, page_url: str) -> str:
    """For study-plan chunks, rewrite the doc_title so citations show the
    admission cohort (`kull HTYY`).

    The chunker emits titles like "CTFYS studieplan: Valbara masterprogram"
    or "CTFYS årskurs 1: behörighetsgivande kurser". This helper extracts the
    programme name (the prefix before "studieplan:" / "årskurs N") and
    rewrites the title to "<programme_name>, Utbildningsplan kull HT23".
    Leaves the title untouched when the URL doesn't fit a programme-term
    pattern or the prefix can't be parsed.
    """
    if not doc_title:
        return doc_title
    path = urlsplit(_canonicalize(page_url)).path
    m = _PROGRAM_BUNDLE_BASE_RE.match(path)
    if not m:
        return doc_title
    term_match = re.search(r"/(\d{5})(?:/|$)", path)
    if not term_match:
        return doc_title
    kull = _kull_label_sv(term_match.group(1))
    if not kull:
        return doc_title
    name_match = _KULL_TITLE_PROGRAMME_RE.match(doc_title)
    programme_name = (name_match.group(1) if name_match else doc_title).strip()
    if not programme_name:
        return doc_title
    return f"{programme_name}, Utbildningsplan kull {kull}"


def _studyplan_bundle_base_url(page_url: str) -> str:
    """Strip any sidebar suffix (`/arskursN`, `/omfattning`, `/inriktningar`,
    …) from a programme-term URL so all chunks within one study-plan bundle
    share a single canonical citation target.

    The KTH SPA renders the same studyProgramme JSON on every sidebar route,
    so deep-linking to a per-section URL would just confuse a user who
    clicked the citation expecting to find that specific section.

    Returns the original URL when the path doesn't fit the
    `/student/kurser/program/<CODE>/<TERM>[/<slug>]` pattern.
    """
    canonical = _canonicalize(page_url)
    path = urlsplit(canonical).path
    m = _PROGRAM_BUNDLE_BASE_RE.match(path)
    if not m:
        return canonical
    return _canonicalize(f"https://{_KTH_HOST}{m.group(1)}")


def _build_studyplan_chunk(
    *,
    text: str,
    page_url: str,
    fragment: str,
    section_path: str,
    doc_title: str,
    fetched_at: int,
    is_stale: bool = False,
) -> RetrievedChunk:
    if not text:
        raise ValueError("empty chunk text")
    rel_source = f"{page_url}#{fragment}" if fragment else page_url
    chunk_id = f"web:{rel_source}"
    # Citations should link to the study-plan bundle as a whole, not the
    # per-section sidebar route — the KTH SPA renders the same JSON on every
    # sidebar URL, so a per-section deep link would just confuse a user who
    # opens it expecting to find that specific section. The non-bundle path
    # falls back to `page_url` (e.g. /student/kurser/kurs/... course pages).
    canonical_source = _studyplan_bundle_base_url(page_url)
    # Rewrite title to include the admission cohort ("kull HT23") for
    # study-plan chunks so the Sources block disambiguates between years
    # when the same programme has chunks from multiple admission terms.
    display_title = _study_plan_title_with_kull(doc_title, page_url)
    return RetrievedChunk(
        chunk_id=chunk_id,
        text=_truncate_studyplan_text(text),
        rel_source=rel_source,
        doc_title=display_title,
        doc_type="html",
        language="sv",
        section_path=section_path,
        chunk_index=0,
        chroma_distance=0.0,
        rerank_score=2.5 if is_stale else 3.5,
        source_url=canonical_source,
        fetched_at=fetched_at,
        is_stale=is_stale,
    )


def _studyplan_chunks_from_studyprogramme(
    sp: dict,
    *,
    programme_name: str,
    page_url: str,
    fetched_at: int,
    is_stale: bool,
    lang: str = "sv",
) -> list[RetrievedChunk]:
    """One chunk per non-empty studyProgramme.<field>. Atlas labels the section."""
    from student_bot.bot.study_plan_atlas import get_atlas

    atlas = get_atlas()
    out: list[RetrievedChunk] = []
    for key, raw in sp.items():
        text = _flatten_text(raw)
        if len(text) < 20:  # skip empty / boilerplate stubs
            continue
        atlas_label = atlas.label_for_field(key, lang)
        topic_label = atlas_label or key
        # When the atlas labelled the field, the human-readable label IS the
        # section. The raw JSON key (`fält: <key>`) is noise that bloats the
        # citation tag and the section_path-based dedup key, and trips up the
        # LLM's exact-copy citation matcher. Keep the field name visible only
        # when the atlas didn't recognise the key, so an unlabelled field
        # still has *some* identity.
        section_path = topic_label if atlas_label else f"{topic_label} (fält: {key})"
        title_prefix = programme_name.strip() if programme_name else "Studieplan"
        doc_title = f"{title_prefix} studieplan: {topic_label}"
        try:
            out.append(
                _build_studyplan_chunk(
                    text=text,
                    page_url=page_url,
                    fragment=key,
                    section_path=section_path,
                    doc_title=doc_title,
                    fetched_at=fetched_at,
                    is_stale=is_stale,
                )
            )
        except ValueError:
            continue
        if len(out) >= _MAX_STUDYPLAN_CHUNKS_PER_PAGE:
            break
    return out


def _studyplan_chunks_from_year_page(
    store: dict,
    *,
    programme_name: str,
    year: int,
    page_url: str,
    fetched_at: int,
    is_stale: bool,
) -> list[RetrievedChunk]:
    """Per-bucket chunks from /arskursN – one per non-empty Valvillkor group."""
    curriculums = store.get("curriculums") if isinstance(store, dict) else None
    if not isinstance(curriculums, list) or not curriculums:
        return []
    cy0 = curriculums[0]
    if not isinstance(cy0, dict):
        return []
    study_years = cy0.get("studyYears") or []
    target = next(
        (y for y in study_years if isinstance(y, dict) and int(y.get("yearNumber") or 0) == year),
        None,
    )
    if not isinstance(target, dict):
        return []

    free_text_lines: list[str] = []
    for ft in target.get("freeTexts") or []:
        if isinstance(ft, dict):
            tx = ft.get("Text")
            if isinstance(tx, str) and tx.strip():
                free_text_lines.append(_strip_html_to_text(tx))

    raw_courses = target.get("courses")
    out: list[RetrievedChunk] = []

    # If this year's page carries a "behörighetsgivande kurser per
    # masterprogram" block, emit it as a dedicated, high-priority chunk so
    # retrieval doesn't bury it inside the per-Valvillkor course tables.
    elig_text = _eligibility_text_from_store(store)
    if elig_text:
        title_prefix = programme_name.strip() if programme_name else "Studieplan"
        elig_body = (
            f"## Årskurs {year} – behörighetsgivande kurser per masterprogram\n\n{elig_text}"
        )
        try:
            out.append(
                _build_studyplan_chunk(
                    text=elig_body,
                    page_url=page_url,
                    fragment=f"arskurs{year}-behorighet-master",
                    section_path=(f"Årskurs {year} – behörighetsgivande kurser per masterprogram"),
                    doc_title=f"{title_prefix} årskurs {year}: behörighetsgivande kurser",
                    fetched_at=fetched_at,
                    is_stale=is_stale,
                )
            )
        except ValueError:
            pass

    # Some programs (e.g. CINEK arskurs1) ship the year-N course list as an
    # HTML string instead of a structured list. We can't bucket by Valvillkor
    # without parsing the HTML, so emit a single chunk preserving the markup
    # text. Years 2+ on the same program switch to structured lists, which the
    # bucket loop below handles normally.
    if isinstance(raw_courses, str) and raw_courses.strip():
        text_body = _strip_html_to_text(raw_courses)
        if len(text_body) >= 20:
            lines: list[str] = [f"## Årskurs {year} – kurslista"]
            if free_text_lines:
                lines.append("\n".join(f"_Notis:_ {t}" for t in free_text_lines))
            lines.append("")
            lines.append(text_body)
            text = "\n".join(lines).strip()
            title_prefix = programme_name.strip() if programme_name else "Årskurs"
            try:
                out.append(
                    _build_studyplan_chunk(
                        text=text,
                        page_url=page_url,
                        fragment=f"arskurs{year}",
                        section_path=f"Årskurs {year} – kurslista",
                        doc_title=f"{title_prefix} årskurs {year}",
                        fetched_at=fetched_at,
                        is_stale=is_stale,
                    )
                )
            except ValueError:
                pass
        return out

    buckets: dict[str, list[dict]] = defaultdict(list)
    for c in raw_courses or []:
        if not isinstance(c, dict):
            continue
        raw_vv = c.get("Valvillkor") or c.get("valvillkor") or "?"
        vv = raw_vv.strip() if isinstance(raw_vv, str) else "?"
        if not vv:
            vv = "?"
        buckets[vv].append(c)

    for vv in _sort_valvillkor_keys(list(buckets.keys())):
        bucket_label = _VALVILLKOR_BUCKET_LABELS_SV.get(vv, f"Valvillkor {vv}")
        rows: list[str] = []
        for c in sorted(buckets[vv], key=lambda x: str(x.get("kod") or "")):
            row = _markdown_course_line_from_curriculum_row(c)
            if row:
                rows.append(row)
        if not rows:
            continue
        lines: list[str] = [f"## Årskurs {year} – {bucket_label} ({vv})"]
        if free_text_lines:
            lines.append("\n".join(f"_Notis:_ {t}" for t in free_text_lines))
        lines.append("")
        lines.extend(rows)
        text = "\n".join(lines).strip()
        title_prefix = programme_name.strip() if programme_name else "Årskurs"
        try:
            out.append(
                _build_studyplan_chunk(
                    text=text,
                    page_url=page_url,
                    fragment=f"arskurs{year}-{vv}",
                    section_path=f"Årskurs {year} – {bucket_label} ({vv})",
                    doc_title=f"{title_prefix} årskurs {year} ({bucket_label})",
                    fetched_at=fetched_at,
                    is_stale=is_stale,
                )
            )
        except ValueError:
            continue
        if len(out) >= _MAX_STUDYPLAN_CHUNKS_PER_PAGE:
            break
    return out


def _studyplan_chunks_from_specializations(
    store: dict,
    *,
    programme_name: str,
    page_url: str,
    fetched_at: int,
    is_stale: bool,
) -> list[RetrievedChunk]:
    sp_field = store.get("specializations") if isinstance(store, dict) else None
    if not isinstance(sp_field, list) or not sp_field:
        # Some programs put specializations under studyProgramme.specialisations.
        sp_obj = store.get("studyProgramme") if isinstance(store, dict) else None
        if isinstance(sp_obj, dict):
            sp_field = sp_obj.get("specialisations")
    if not isinstance(sp_field, list) or not sp_field:
        return []
    out: list[RetrievedChunk] = []
    for item in sp_field:
        if not isinstance(item, dict):
            continue
        kod = str(item.get("kod") or item.get("code") or "").strip()
        ben = str(item.get("benamning") or item.get("name") or "").strip()
        besk = _strip_html_to_text(str(item.get("beskrivning") or item.get("description") or ""))
        body_lines: list[str] = []
        head = " ".join(b for b in [kod, ben] if b)
        if head:
            body_lines.append(f"## {head}")
        if besk:
            body_lines.append(besk)
        text = "\n\n".join(body_lines).strip()
        if len(text) < 10:
            continue
        title_prefix = programme_name.strip() if programme_name else "Inriktning"
        try:
            out.append(
                _build_studyplan_chunk(
                    text=text,
                    page_url=page_url,
                    fragment=f"inriktning-{kod or 'x'}",
                    section_path=f"Inriktning: {kod} {ben}".strip(),
                    doc_title=f"{title_prefix} inriktning: {kod} {ben}".strip(),
                    fetched_at=fetched_at,
                    is_stale=is_stale,
                )
            )
        except ValueError:
            continue
        if len(out) >= _MAX_STUDYPLAN_CHUNKS_PER_PAGE:
            break
    return out


def _studyplan_chunks_from_html(
    html: str,
    *,
    final_url: str,
    fetched_at: int,
    lang: str = "sv",
    is_stale: bool = False,
) -> list[RetrievedChunk]:
    """Walk the SPA store on a /student/kurser/program/... page and emit one
    chunk per logical section (studyProgramme field, year-bucket, or
    specialisation). Returns ``[]`` when the store is missing – callers fall
    back to the legacy single-chunk path.
    """
    store = _compressed_application_store(html)
    if not isinstance(store, dict):
        return []
    programme_name = str(store.get("programmeName") or "").strip()
    path = urlsplit(final_url).path
    is_omfattning_or_genomforande = path.endswith("/omfattning") or path.endswith("/genomforande")
    is_inriktningar = path.endswith("/inriktningar")
    year = _program_page_year_from_url(final_url)

    chunks: list[RetrievedChunk] = []

    if is_omfattning_or_genomforande:
        sp = store.get("studyProgramme")
        if isinstance(sp, dict):
            chunks.extend(
                _studyplan_chunks_from_studyprogramme(
                    sp,
                    programme_name=programme_name,
                    page_url=final_url,
                    fetched_at=fetched_at,
                    is_stale=is_stale,
                    lang=lang,
                )
            )

    if year is not None:
        chunks.extend(
            _studyplan_chunks_from_year_page(
                store,
                programme_name=programme_name,
                year=year,
                page_url=final_url,
                fetched_at=fetched_at,
                is_stale=is_stale,
            )
        )

    if is_inriktningar:
        chunks.extend(
            _studyplan_chunks_from_specializations(
                store,
                programme_name=programme_name,
                page_url=final_url,
                fetched_at=fetched_at,
                is_stale=is_stale,
            )
        )

    return chunks[:_MAX_STUDYPLAN_CHUNKS_PER_PAGE]


def _program_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href:
            continue
        abs_url = _canonicalize(urljoin(base_url, href))
        if "/student/kurser/program/" in abs_url:
            out.append(abs_url)
    # dedupe order
    dedup: list[str] = []
    seen: set[str] = set()
    for u in out:
        if u not in seen:
            seen.add(u)
            dedup.append(u)
    return dedup


def _compressed_application_store(html: str) -> dict | None:
    m = _COMP_STORE_RE.search(html or "")
    if not m:
        return None
    try:
        return json.loads(unquote(m.group(1)))
    except (json.JSONDecodeError, ValueError):
        return None


# Eligibility ("behörighetsgivande kurser") block extraction.
#
# Two formats observed on KTH study-plan pages, both expressing the same
# information — which courses a student must complete to be eligible for a
# given master programme. The block is critical for "what do I take in year 3
# to qualify for the X master?" questions, but it doesn't always exist (some
# programmes don't carry it; some terms have stopped including it). We extract
# it as a dedicated, high-rank chunk so retrieval doesn't bury it in the
# arskursN bucket-list noise — and so the cross-cohort fallback (when a year
# is missing the block) has a clean payload to surface from a prior term.
_ELIGIBILITY_RE = re.compile(r"(?im)beh[öo]righetsgivande\s+kurs(?:er|en)?\b")


def _eligibility_text_from_store(store: dict | None) -> str | None:
    """Return the cleanest behörighetsgivande block text in the SPA store.

    Walks every string field, picks the longest one matching the regex (the
    longer form usually has more master-programme entries), and strips HTML
    to readable text. Returns None when no block exists.
    """
    if not isinstance(store, dict):
        return None
    candidates: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
        elif isinstance(node, str) and _ELIGIBILITY_RE.search(node):
            candidates.append(node)

    _walk(store)
    if not candidates:
        return None
    candidates.sort(key=len, reverse=True)
    text = _strip_html_to_text(candidates[0])
    return text.strip() or None


def _term_label_sv(term: str) -> str:
    """KTH 5-digit programme term -> Swedish label, e.g. '20232' -> 'HT2023'."""
    if not (isinstance(term, str) and re.fullmatch(r"\d{5}", term)):
        return term or ""
    season = "HT" if term[4] == "2" else "VT"
    return f"{season}{term[:4]}"


def _prior_term_year_urls(
    programme_code: str,
    current_term: str,
    year: int,
    store: dict | None,
    *,
    limit: int = 3,
) -> list[str]:
    """Year-page URLs for prior terms, most recent first. Used as fallback when
    the requested term's page lacks the eligibility block."""
    if not (programme_code and re.fullmatch(r"\d{5}", current_term or "")):
        return []
    base = f"https://www.kth.se/student/kurser/program/{programme_code}"
    candidates: list[str] = []
    # Prefer terms the page itself advertises (programmeTerms).
    for t in _normalized_programme_terms_from_store(store):
        if t < current_term:
            candidates.append(f"{base}/{t}/arskurs{year}")
    # Heuristic backstop: if the store had nothing useful, step back through
    # plausible KTH term codes (same season N years prior, then opposite season).
    if not candidates:
        try:
            yr = int(current_term[:4])
            season = current_term[4]
            for delta in (1, 2, 3):
                candidates.append(f"{base}/{yr - delta}{season}/arskurs{year}")
        except (ValueError, IndexError):
            pass
    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= limit:
            break
    return out


def _maybe_emit_fallback_eligibility(
    *,
    html: str,
    final_url: str,
    structured: list,
    cfg: Config,
    patterns,
    cache,
    visited: set,
    chunks: list,
    source_urls: list,
) -> None:
    """If a programme /arskursN page lacks an eligibility chunk, look back to
    the most recent prior cohort that has one and append it as a labeled,
    caveated chunk. No-op when the page already has the block, isn't a year
    page, or no prior term yields a block.

    Caps at one successful fallback fetch per call. Failed fetches log and
    move on; we never raise out of this best-effort enrichment.
    """
    m = re.search(r"/program/([A-Z]{5})/(\d{5})/arskurs(\d+)/?$", urlsplit(final_url).path)
    if not m:
        return
    if any(("behörighetsgivande" in (c.section_path or "").lower()) for c in structured):
        return
    programme_code, current_term, year_str = m.group(1), m.group(2), m.group(3)
    try:
        year = int(year_str)
    except ValueError:
        return
    store = _compressed_application_store(html)
    candidates = _prior_term_year_urls(programme_code, current_term, year, store, limit=3)
    requested_label = _term_label_sv(current_term)
    for prior_url in candidates:
        if prior_url in visited:
            continue
        if not _is_allowed_url(prior_url, cfg, patterns):
            continue
        visited.add(prior_url)
        try:
            p_final, p_html = _fetch_html(prior_url, cfg)
        except Exception as e:
            log.info("eligibility-fallback: fetch failed for %s: %s", prior_url, e)
            continue
        if not _is_allowed_url(p_final, cfg, patterns):
            continue
        p_store = _compressed_application_store(p_html)
        elig_text = _eligibility_text_from_store(p_store)
        if not elig_text:
            log.info("eligibility-fallback: no block on %s", p_final)
            continue
        # Resolve the prior term's label from the URL itself so the citation
        # tells the user exactly which cohort the block came from.
        pm = re.search(r"/program/[A-Z]{5}/(\d{5})/arskurs", urlsplit(p_final).path)
        prior_term = pm.group(1) if pm else current_term
        prior_label = _term_label_sv(prior_term)
        programme_name = ""
        if isinstance(p_store, dict):
            programme_name = str(p_store.get("programmeName") or "").strip()
        title_prefix = programme_name or "Studieplan"
        # The caveat is intentionally bilingual-light Swedish only because
        # the underlying corpus block is Swedish. The bot's reply prompt
        # then renders this content in either lang.
        body = (
            "## Årskurs {year} – behörighetsgivande kurser per masterprogram "
            "(från läsåret {prior})\n\n"
            "**OBS!** Listan nedan är hämtad från utbildningsplanen för **{prior}** "
            "eftersom **{requested}** ännu inte innehåller motsvarande avsnitt. "
            "Reglerna är ofta stabila mellan årgångar men kan ha ändrats. "
            "Verifiera mot ditt eget läsår eller med studievägledaren.\n\n"
            "{elig}"
        ).format(year=year, prior=prior_label, requested=requested_label, elig=elig_text)
        now = int(time.time())
        cache.put(CachedPage(url=p_final, title=title_prefix, content=body, fetched_at=now))
        try:
            chunk = _build_studyplan_chunk(
                text=body,
                page_url=p_final,
                fragment=f"arskurs{year}-behorighet-master-fallback",
                section_path=(
                    f"Årskurs {year} – behörighetsgivande kurser per masterprogram "
                    f"(läsår {prior_label})"
                ),
                doc_title=(
                    f"{title_prefix} årskurs {year}: behörighetsgivande kurser ({prior_label})"
                ),
                fetched_at=now,
                is_stale=False,
            )
        except ValueError:
            continue
        chunks.append(chunk)
        if p_final not in source_urls:
            source_urls.append(p_final)
        log.info(
            "eligibility-fallback: emitted block from %s -> requested %s",
            prior_label,
            requested_label,
        )
        return  # cap at one successful fallback


def _normalized_programme_terms_from_store(store: dict | None) -> list[str]:
    if not store:
        return []
    raw = store.get("programmeTerms") or []
    out: list[str] = []
    for t in raw:
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, dict):
            for k in ("term", "code", "id"):
                if t.get(k) is not None:
                    out.append(str(t[k]))
                    break
    uniq = sorted(
        {x for x in (str(y) for y in out) if re.fullmatch(r"\d{5}", x)},
        reverse=True,
    )
    return uniq


def _is_program_root_only_url(url: str) -> bool:
    path = urlsplit(_canonicalize(url)).path.rstrip("/") or "/"
    return bool(re.fullmatch(r"/student/kurser/program/[A-Za-z0-9]+", path))


def _program_segment_code(url: str) -> str | None:
    parts = urlsplit(url).path.strip("/").split("/")
    try:
        i = parts.index("program")
        code = parts[i + 1]
        return code.upper() if code else None
    except (ValueError, IndexError):
        return None


def _intake_year_bounds_from_terms(terms: list[str]) -> tuple[int, int] | None:
    """Map each KTH five-digit programme period to an approximate calendar start year (first four digits)."""
    years: list[int] = []
    for t in terms:
        s = str(t)
        if not re.fullmatch(r"\d{5}", s):
            continue
        try:
            years.append(int(s[:4]))
        except ValueError:
            continue
    if not years:
        return None
    return min(years), max(years)


def _has_spring_intake(terms: list[str]) -> bool:
    """True if any 5-digit term ends in `1` (spring/VT). Civilingenjör programmes
    never satisfy this – they only admit in autumn – so the clarification example
    suppresses the VT prompt for them."""
    for t in terms:
        s = str(t)
        if re.fullmatch(r"\d{5}", s) and s[4] == "1":
            return True
    return False


def _clarify_program_terms_sv(program_code: str, terms: list[str]) -> str:
    span = _intake_year_bounds_from_terms(terms)
    span_line = ""
    if span:
        y0, y1 = span
        if y0 == y1:
            span_line = f"\nJust nu finns webbdata för ungefär **{y0}** som startår."
        else:
            span_line = f"\nJust nu finns webbdata för år mellan **{y0}** och **{y1}**."
    example = "**HT2024** eller **VT2025**" if _has_spring_intake(terms) else "**HT2024**"
    return (
        f"För att visa rätt utbildningsplan för **{program_code}** behöver jag veta "
        f"vilken antagningsomgång som gäller. Skriv gärna t.ex. {example}."
        f"{span_line}"
    )


_PROGRAM_TERMS_CACHE: dict[str, tuple[float, list[str]]] = {}
_PROGRAM_TERMS_CACHE_TTL_SECONDS = 6 * 3600.0


def _cached_terms_for_code(cfg: Config, code: str) -> list[str]:
    """Fetch the programme root once per code (6h TTL) and parse its terms list.

    Used by the multi-candidate disambiguator to decide whether each candidate
    is currently active. Errors are cached as empty so a transient outage does
    not silently mark a program as discontinued.
    """
    now = time.time()
    cached = _PROGRAM_TERMS_CACHE.get(code)
    if cached and now - cached[0] < _PROGRAM_TERMS_CACHE_TTL_SECONDS:
        return cached[1]
    url = _canonicalize(f"https://{_KTH_HOST}/student/kurser/program/{code}")
    try:
        _, html = _fetch_html(url, cfg)
    except Exception as e:
        log.warning("dynamic-web: terms fetch failed for %s: %s", code, e)
        _PROGRAM_TERMS_CACHE[code] = (now, [])
        return []
    store = _compressed_application_store(html)
    terms = _normalized_programme_terms_from_store(store)
    _PROGRAM_TERMS_CACHE[code] = (now, terms)
    return terms


def _level_label_sv(level: str) -> str:
    return {
        "civilingenjor": "civilingenjör, 5 år",
        "master": "masterprogram, 2 år",
        "hogskoleingenjor": "högskoleingenjör, 3 år",
        "bachelor": "kandidatprogram, 3 år",
    }.get(level, "")


def _level_label_en(level: str) -> str:
    return {
        "civilingenjor": "Master of Science in Engineering, 5 years",
        "master": "Master's programme, 2 years",
        "hogskoleingenjor": "Bachelor of Science in Engineering, 3 years",
        "bachelor": "Bachelor's programme, 3 years",
    }.get(level, "")


def _canonical_alias_for_code(aliases: dict[str, str], code: str, lang: str) -> str:
    """Pick the most informative human-readable alias for this code."""
    matches = [a for a, c in aliases.items() if c == code and a and a.upper() != code]
    if not matches:
        return code
    if lang == "en":
        prefs = ("degree programme", "master's programme", "master programme", "bachelor")
        en_likely = [a for a in matches if any(p in a for p in prefs)]
        if en_likely:
            return max(en_likely, key=len)
    sv_prefs = (
        "civilingenjörsutbildning",
        "masterprogram",
        "kandidatprogram",
        "högskoleingenjörsutbildning",
    )
    sv_likely = [a for a in matches if any(a.startswith(p) for p in sv_prefs)]
    if sv_likely:
        return max(sv_likely, key=len)
    return max(matches, key=len)


def _build_multi_program_clarification(
    candidates: list[tuple[str, list[str], str]], aliases: dict[str, str]
) -> tuple[str, str]:
    """Bilingual 'which program do you mean?' message. `candidates` items are
    (code, terms, status) where status is 'current' or 'historical'."""

    def _line_sv(code: str, terms: list[str], status: str) -> str:
        name = _canonical_alias_for_code(aliases, code, "sv")
        if name and name != code:
            name = name[:1].upper() + name[1:]
        level = _level_label_sv(_program_level(code))
        bits = [f"**{code}**"]
        if name and name != code:
            bits.append(name)
        if level:
            bits.append(f"({level})")
        if status == "historical":
            span = _intake_year_bounds_from_terms(terms)
            bits.append(
                f"– avvecklat, senaste antagning {span[1]}" if span else "– avvecklat program"
            )
        return "- " + " ".join(bits)

    def _line_en(code: str, terms: list[str], status: str) -> str:
        name = _canonical_alias_for_code(aliases, code, "en")
        if name and name != code:
            name = name[:1].upper() + name[1:]
        level = _level_label_en(_program_level(code))
        bits = [f"**{code}**"]
        if name and name != code:
            bits.append(name)
        if level:
            bits.append(f"({level})")
        if status == "historical":
            span = _intake_year_bounds_from_terms(terms)
            bits.append(f"– discontinued, last intake {span[1]}" if span else "– discontinued")
        return "- " + " ".join(bits)

    sv = (
        "Ditt program är inte entydigt. Vilket av följande menar du? "
        "Svara gärna med koden eller hela namnet:\n"
        + "\n".join(_line_sv(c, t, s) for c, t, s in candidates)
    )
    en = (
        "Your program reference is ambiguous. Which one do you mean? "
        "Reply with the code or the full name:\n"
        + "\n".join(_line_en(c, t, s) for c, t, s in candidates)
    )
    return sv, en


@dataclass
class _MultiCandidateResolution:
    queue_urls: list[str] = field(default_factory=list)
    clarification_sv: str = ""
    clarification_en: str = ""
    resolved_code: str | None = None


def _current_calendar_year() -> int:
    import datetime as _dt

    return _dt.datetime.now().year


def _resolve_multi_program_candidates(
    cfg: Config, question: str, prog_roots: list[str]
) -> _MultiCandidateResolution:
    """Decide between several candidate program codes by intake-year recency
    and discriminative-token bypass. Returns either a narrowed program list
    or a 'which one?' clarification."""
    qn = _norm(question)
    q_tokens = set(re.findall(r"[a-z0-9åäö]+", qn))
    aliases = _get_program_aliases(cfg)

    cutoff = _current_calendar_year() - cfg.dynamic_web.historical_program_years
    candidates: list[tuple[str, list[str], str]] = []
    seen: set[str] = set()
    for root in prog_roots:
        code = _program_segment_code(root)
        if not code or code in seen:
            continue
        seen.add(code)
        terms = _cached_terms_for_code(cfg, code)
        candidates.append((code, terms, ""))

    if not candidates:
        return _MultiCandidateResolution(queue_urls=list(prog_roots))

    def is_current(terms: list[str]) -> bool:
        bounds = _intake_year_bounds_from_terms(terms)
        if not bounds:
            return True  # unknown – treat as current
        return bounds[1] >= cutoff

    has_current = any(is_current(t) for _, t, _ in candidates)

    verbatim_typed = set(_PROGRAM_CODE_RE.findall(question))
    token_freq = _alias_token_frequency(aliases)
    rare_token_threshold = cfg.dynamic_web.discriminator_rare_token_max_aliases

    def discriminator_present(code: str) -> bool:
        """User typed enough discriminative tokens to override the
        historical-program suppression.

        Requires: full match of an alias's strong-token set AND at least one
        of those matched tokens being *rare* across the alias corpus
        (appearing in ≤ `discriminator_rare_token_max_aliases` aliases). The
        rarity gate stops a discontinued programme from being rescued by a
        query that only mentions a common subject term like `matematik` —
        but `fusionsenergi` (unique to TFEPM) still rescues that one.
        """
        for alias, c in aliases.items():
            if c != code or not alias:
                continue
            strong = _alias_strong_tokens(alias)
            if not strong:
                continue
            matched = strong & q_tokens
            if len(matched) != len(strong):
                continue
            if any(token_freq.get(t, 0) <= rare_token_threshold for t in matched):
                return True
        return False

    if has_current:
        kept: list[tuple[str, list[str], str]] = []
        dropped: list[str] = []
        for code, terms, _ in candidates:
            if is_current(terms):
                kept.append((code, terms, "current"))
            elif code in verbatim_typed or discriminator_present(code):
                kept.append((code, terms, "historical"))
            else:
                dropped.append(code)
        if dropped:
            log.info(
                "dynamic-web: hid historical candidates with last intake < %d: %s",
                cutoff,
                dropped,
            )
        candidates = kept
    else:
        candidates = [(c, t, "historical") for c, t, _ in candidates]

    if len(candidates) == 0:
        return _MultiCandidateResolution(queue_urls=list(prog_roots))
    if len(candidates) == 1:
        code = candidates[0][0]
        return _MultiCandidateResolution(
            queue_urls=[f"https://{_KTH_HOST}/student/kurser/program/{code}"],
            resolved_code=code,
        )

    sv, en = _build_multi_program_clarification(candidates, aliases)
    return _MultiCandidateResolution(clarification_sv=sv, clarification_en=en)


def _clarify_program_terms_en(program_code: str, terms: list[str]) -> str:
    span = _intake_year_bounds_from_terms(terms)
    span_line = ""
    if span:
        y0, y1 = span
        if y0 == y1:
            span_line = f"\nStudy-plan pages on the web cover roughly intake year **{y0}**."
        else:
            span_line = (
                f"\nStudy-plan pages on the web cover roughly years **{y0}** through **{y1}**."
            )
    example = (
        "**autumn intake (HT2024)** or **spring (VT2025)**"
        if _has_spring_intake(terms)
        else "**autumn intake (HT2024)**"
    )
    return (
        f"To show the right study plan for **{program_code}**, which **admission round** applies to you? "
        f"Please mention e.g. {example}."
        f"{span_line}"
    )


def _select_programme_urls(
    program_code: str,
    sorted_terms_desc: list[str],
    hints: AdmissionHints,
    year_level: int | None = None,
    question: str | None = None,
) -> ProgrammeRootResolution:
    """Pick concrete /program/<code>/<term> URLs or ask for clarification."""
    root = _canonicalize(f"https://{_KTH_HOST}/student/kurser/program/{program_code}")
    terms = sorted_terms_desc
    year_suffix = f"/arskurs{year_level}" if year_level else ""

    if not terms:
        return ProgrammeRootResolution(queue_urls=[f"{root}{year_suffix}"])

    if hints.exact_term and hints.exact_term in terms:
        return ProgrammeRootResolution(queue_urls=[f"{root}/{hints.exact_term}{year_suffix}"])

    cands = list(terms)
    if hints.year_prefix:
        cands = [t for t in terms if t.startswith(hints.year_prefix)]
    elif hints.exact_term is None:
        if len(terms) == 1:
            return ProgrammeRootResolution(queue_urls=[f"{root}/{terms[0]}{year_suffix}"])
        if question and _question_is_year_independent(question):
            log.info(
                "dynamic-web: %s year-independent question; using newest term %s without asking",
                program_code,
                terms[0],
            )
            return ProgrammeRootResolution(queue_urls=[f"{root}/{terms[0]}{year_suffix}"])
        return ProgrammeRootResolution(
            queue_urls=[],
            clarification_sv=_clarify_program_terms_sv(program_code, terms),
            clarification_en=_clarify_program_terms_en(program_code, terms),
        )

    if len(cands) == 1:
        return ProgrammeRootResolution(queue_urls=[f"{root}/{cands[0]}{year_suffix}"])
    if not cands:
        return ProgrammeRootResolution(
            queue_urls=[],
            clarification_sv=_clarify_program_terms_sv(program_code, terms)
            + f"\n_(Din sökning matchade ingen period som börjar på {hints.year_prefix}.)_",
            clarification_en=_clarify_program_terms_en(program_code, terms)
            + f"\n_(Nothing starting with programme period **{hints.year_prefix}** was found.)_",
        )

    preview = ", ".join(cands[:12])
    return ProgrammeRootResolution(
        queue_urls=[],
        clarification_sv=(
            f"För **{program_code}** finns flera omgångar som matchar året **{hints.year_prefix}** "
            f"({preview}). Vilken femsiffrig periodkod gäller dig?"
        ),
        clarification_en=(
            f"For **{program_code}**, several rounds match **{hints.year_prefix}** "
            f"({preview}). Which five-digit period code applies?"
        ),
    )


def _resolve_program_root_targets(
    cfg: Config,
    programme_root_url: str,
    hints: AdmissionHints,
    year_level: int | None = None,
    question: str | None = None,
) -> ProgrammeRootResolution:
    code = _program_segment_code(programme_root_url)
    if not code:
        return ProgrammeRootResolution(queue_urls=[_canonicalize(programme_root_url)])

    try:
        _, html = _fetch_html(_canonicalize(programme_root_url), cfg)
    except Exception as e:
        log.warning("dynamic-web: programme root fetch failed for %s: %s", programme_root_url, e)
        return ProgrammeRootResolution(queue_urls=[_canonicalize(programme_root_url)])

    title, _ = _sanitize_to_text(html)
    store = _compressed_application_store(html)
    if _programme_root_title_is_unknown_code_shell(title):
        log.info("dynamic-web: programme root appears to be unknown / missing for %s", code)
        return ProgrammeRootResolution(queue_urls=[], missing_program_codes=(code,))

    terms = _normalized_programme_terms_from_store(store)

    if store and isinstance(store.get("programmeCode"), str):
        mc = store["programmeCode"].strip().upper()
        if re.fullmatch(r"[A-Z]{5}", mc):
            code = mc

    resolved = _select_programme_urls(code, terms, hints, year_level=year_level, question=question)
    log.info(
        "dynamic-web: programme %s terms=%s hints=%s -> %s",
        code,
        terms,
        hints,
        resolved.queue_urls or "ASK",
    )
    return resolved


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        c = _canonicalize(u)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _programme_term_bundle_urls(url: str) -> list[str]:
    """For /program/<CODE>/<TERM>(/arskursN), return a stable study-plan bundle.

    Order matters: the fetcher visits URLs in this order and stops at
    ``max_pages_per_query``. ``/omfattning`` carries the full
    ``studyProgramme.*`` payload that the per-section chunker expands into
    ~25 small chunks, so it comes right after the base term page. Year
    pages follow with their concrete Valvillkor data. ``/inriktningar``
    uses a separate chunker path for specialisations.

    Redundant slugs (``/genomforande``, ``/mal``, ``/behorighet``,
    ``/kurslista``) are intentionally omitted — see
    ``_PROGRAM_SIDEBAR_SLUGS_REDUNDANT``.
    """
    path = urlsplit(_canonicalize(url)).path
    m = _PROGRAM_TERM_RE.fullmatch(path)
    if not m:
        return [_canonicalize(url)]
    code, term, _year = m.group(1).upper(), m.group(2), m.group(3)
    base = _canonicalize(f"https://{_KTH_HOST}/student/kurser/program/{code}/{term}")
    kept_sidebar = tuple(
        slug for slug in _PROGRAM_SIDEBAR_SLUGS if slug not in _PROGRAM_SIDEBAR_SLUGS_REDUNDANT
    )
    primary_sidebar = ("omfattning",)
    other_sidebar = tuple(slug for slug in kept_sidebar if slug not in primary_sidebar)
    out: list[str] = [base]
    out.extend([f"{base}/{slug}" for slug in primary_sidebar])
    out.extend([f"{base}/arskurs{n}" for n in range(1, 6)])
    out.extend([f"{base}/{slug}" for slug in other_sidebar])
    return _dedupe_urls(out)


def _is_redundant_sidebar_url(url: str) -> bool:
    """True if `url` is a /program/<CODE>/<TERM>/<slug> page whose chunks
    duplicate (or are subsumed by) /omfattning. Used to keep link discovery
    from re-injecting the slugs we pruned from the default bundle."""
    path = urlsplit(_canonicalize(url)).path
    parts = [p for p in path.split("/") if p]
    if len(parts) < 5:
        return False
    # parts: ["student", "kurser", "program", CODE, TERM, slug?]
    if parts[:3] != ["student", "kurser", "program"]:
        return False
    if len(parts) < 6:
        return False
    return parts[5] in _PROGRAM_SIDEBAR_SLUGS_REDUNDANT


def corpus_programme_substrings_for_query(q: str) -> frozenset[str] | None:
    """Substring filters for corpus rel_source/doc paths when cohort hints appear in-program."""
    if not program_study_intent_question(q):
        return None
    h = parse_program_admission_hints(q)
    out: set[str] = set()
    if h.exact_term:
        out.add(h.exact_term)
    if h.year_prefix:
        out.add(h.year_prefix)
    return frozenset(out) if out else None


def _explicit_unknown_programme_codes(question: str, cfg: Config) -> list[str]:
    """5-letter programme-style tokens not present in the kurser-inom-program snapshot.

    Unknown codes are not expanded into /program/<CODE> URLs; without this check the
    pipeline would fall through to corpus RAG and emit a vague refusal.
    """
    if not program_study_intent_question(question):
        return []
    aliases = _get_program_aliases(cfg)
    known_codes = {
        str(v).upper() for v in aliases.values() if re.fullmatch(r"[A-Z]{5}", str(v).upper())
    }
    if not known_codes:
        return []
    out: list[str] = []
    for code in _PROGRAM_CODE_RE.findall(question):
        if code not in known_codes:
            out.append(code)
    return list(dict.fromkeys(out))


_CONTACT_INTENT_RE = re.compile(
    r"\b("
    # Swedish role keywords — these people don't live on study-plan pages,
    # so the dynamic_web programme-alias path can't answer "vem är PA för X".
    r"programansvarig(?:a|e|en)?"
    r"|studievägledar(?:e|en|na)"
    r"|studievägledning(?:en)?"
    r"|studierektor(?:n|er|erna)?"
    r"|utbildningskansli(?:et)?"
    # English equivalents
    r"|programme\s+director(?:s)?"
    r"|program\s+director(?:s)?"
    r"|study\s+coun[sc]ell?or(?:s)?"
    r"|study\s+coun[sc]elling"
    r"|director\s+of\s+studies"
    r")\b",
    re.IGNORECASE,
)


def _is_contact_intent_question(question: str) -> bool:
    """Return True when the question is asking about a person/office whose
    contact info lives in the indexed corpus, not in a study plan. Such
    questions should bypass the dynamic_web programme-alias flow — which
    would otherwise hijack "Vem är programansvarig för teknisk fysik?"
    into "CTFYS or TTFYM?" → "HT-year?" and never reach retrieval.
    """
    return bool(_CONTACT_INTENT_RE.search(question or ""))


def maybe_fetch_dynamic_web(
    cfg: Config,
    question: str,
    _lang: str = "sv",
    *,
    program_prior: str | None = None,
    admission_term_prior: str | None = None,
    admission_year_prefix_prior: str | None = None,
) -> WebFetchResult | None:
    if not cfg.dynamic_web.enabled:
        return None

    if _is_contact_intent_question(question):
        return None

    unknown_codes = _explicit_unknown_programme_codes(question, cfg)
    if unknown_codes:
        return WebFetchResult(
            missing_kth_program=_bilingual_missing_kth_program_message(unknown_codes),
        )

    # Local import to avoid a circular dependency at module load time
    # (course_resolver imports a couple of helpers from this module).
    from student_bot.bot.course_resolver import (
        question_has_course_intent,
        question_has_explicit_course_code,
        resolve_course_intent,
    )

    patterns = _compiled_patterns(cfg)
    targets = _extract_targets_with_cfg(question, cfg, program_prior=program_prior)
    course_intent_no_code = question_has_course_intent(
        question
    ) and not question_has_explicit_course_code(question)

    # Master-eligibility routing (issue #55, topic 3). The KTH study-plan
    # SPA only renders "behörighetsgivande kurser per masterprogram" on the
    # civilingenjör programme's `/arskursN` pages — the master programme's
    # own page does NOT list it. So when the question is master-eligibility
    # shaped, force the relevant civilingenjör's URL into prog_roots — or
    # ask which civilingenjör the user studies if we can't tell. Has to run
    # BEFORE the empty-targets early-return below so a bare query like
    # "vilka mappade masterprogram finns?" doesn't fall through to Chroma
    # (which would pick the generic /mappade-masterprogram overview page,
    # not the user's actual study plan).
    course_urls = [t for t in targets if "/student/kurser/kurs/" in t]
    prog_roots = [t for t in targets if _is_program_root_only_url(t)]
    if _question_is_master_eligibility(question):
        civ_code: str | None = None
        if program_prior and _is_civilingenjor_code(program_prior, cfg):
            civ_code = program_prior.strip().upper()
        else:
            for existing in prog_roots:
                code = _program_segment_code(existing)
                if code and _is_civilingenjor_code(code, cfg):
                    civ_code = code
                    break
        if civ_code:
            civ_url = _canonicalize(f"https://{_KTH_HOST}/student/kurser/program/{civ_code}")
            if civ_url not in prog_roots:
                prog_roots.insert(0, civ_url)
                log.info(
                    "dynamic-web: master-eligibility question; routed to civ %s",
                    civ_code,
                )
        else:
            log.info("dynamic-web: master-eligibility question with no civ in memory; asking")
            return WebFetchResult(
                clarification=(
                    "För att svara om behörighet till masterprogram behöver jag "
                    "veta vilket **civilingenjörsprogram** du läser. Skriv t.ex. "
                    "”CTFYS” eller ”civilingenjör i teknisk fysik”.",
                    "To answer about master-programme eligibility I need to know "
                    "which **civilingenjör programme** you study. Reply e.g. "
                    "“CTFYS” or “civilingenjör in engineering physics”.",
                ),
            )

    if not targets and not (course_intent_no_code and program_prior):
        return None
    log.info("dynamic-web: targets=%s", targets)

    hints = parse_program_admission_hints(question)
    # Fall back to a persisted admission hint when this turn doesn't carry
    # one. A user who clarified "HT2024" three turns ago shouldn't have to
    # repeat it on every follow-up. The prior is only used when this turn's
    # own parse came back empty — a fresh hint always wins.
    if not (hints.exact_term or hints.year_prefix):
        if admission_term_prior:
            hints = AdmissionHints(exact_term=admission_term_prior)
        elif admission_year_prefix_prior:
            hints = AdmissionHints(year_prefix=admission_year_prefix_prior)
    year_level = _parse_programme_year_level(question)

    # When the question's wording matches more than one program code, decide
    # between them by intake-year recency before asking about admission round
    # for any single candidate. Otherwise we'd silently pick (and ask about)
    # whichever code happened to be sorted first — possibly a discontinued one.
    if len(prog_roots) > 1:
        multi = _resolve_multi_program_candidates(cfg, question, prog_roots)
        if multi.clarification_sv:
            return WebFetchResult(
                clarification=(multi.clarification_sv, multi.clarification_en),
            )
        if multi.queue_urls:
            prog_roots = list(multi.queue_urls)

    queue: list[str] = list(course_urls)
    resolved_program_code: str | None = None
    for root in prog_roots:
        res = _resolve_program_root_targets(
            cfg, root, hints, year_level=year_level, question=question
        )
        if res.missing_program_codes:
            return WebFetchResult(
                missing_kth_program=_bilingual_missing_kth_program_message(
                    list(res.missing_program_codes),
                ),
            )
        if res.clarification_sv:
            return WebFetchResult(
                clarification=(res.clarification_sv, res.clarification_en),
                resolved_program_code=_program_segment_code(root),
            )
        for u in res.queue_urls:
            queue.extend(_programme_term_bundle_urls(u))
        if resolved_program_code is None:
            resolved_program_code = _program_segment_code(root)

    # Course-without-code path. Triggers when the question references a course
    # by name (no explicit code) and a program code is known — either
    # established this turn or carried over via program_prior.
    if course_intent_no_code:
        course_res = resolve_course_intent(
            cfg,
            question,
            program_prior=program_prior,
            program_now=resolved_program_code,
        )
        if course_res is not None:
            if course_res.clarification_sv:
                return WebFetchResult(
                    clarification=(
                        course_res.clarification_sv,
                        course_res.clarification_en,
                    ),
                    resolved_program_code=resolved_program_code,
                )
            for cu in course_res.course_urls:
                if cu not in queue:
                    queue.append(cu)

    queue = _dedupe_urls(queue)
    if not queue:
        return None

    cache = WebCache(cfg)
    chunks: list[RetrievedChunk] = []
    source_urls: list[str] = []
    used_stale = False
    stale_days = 0
    max_pages = max(1, min(cfg.dynamic_web.max_pages_per_query, cfg.dynamic_web.max_links_followed))
    queue = queue[: max_pages * 2]
    visited: set[str] = set()
    invalid_course_codes: list[str] = []

    while queue and len(visited) < max_pages:
        target = queue.pop(0)
        if target in visited:
            continue
        visited.add(target)
        if not _is_allowed_url(target, cfg, patterns):
            log.warning("dynamic-web: blocked non-allowlisted target %s", target)
            continue

        try:
            log.info("dynamic-web: fetching %s", target)
            final_url, html = _fetch_html(target, cfg)
            if not _is_allowed_url(final_url, cfg, patterns):
                raise ValueError("redirect target outside allowlist")
            if final_url != target:
                log.info("dynamic-web: fetch redirected %s -> %s", target, final_url)
            title, content = _sanitize_to_text(html)
            if "/student/kurser/kurs/" in final_url and _is_kth_placeholder_course_shell(title):
                code_hit = _kth_course_code_from_course_url(final_url)
                if code_hit:
                    invalid_course_codes.append(code_hit)
                log.info(
                    "dynamic-web: skipping KTH placeholder course page (unknown code) %s",
                    final_url,
                )
                continue
            if "/student/kurser/program/" in final_url:
                content = _programme_page_text_with_store(html, content, final_url)
            content = _truncate_web_chunk_text(content)
            # Validate program fetches: if a requested program code maps to a page
            # whose visible heading mentions another code, treat it as mismatch.
            req_m = _PROGRAM_URL_CODE_RE.search(urlsplit(target).path)
            if req_m:
                req_code = req_m.group(1)
                text_codes = set(re.findall(r"\b[A-Z]{5}\b", f"{title}\n{content[:1200]}"))
                if text_codes and req_code not in text_codes:
                    raise ValueError(
                        f"program page mismatch: requested {req_code}, page mentions {sorted(text_codes)}"
                    )
            now = int(time.time())
            cache.put(CachedPage(url=final_url, title=title, content=content, fetched_at=now))
            log.info("dynamic-web: cached %s", final_url)
            # Programme pages: prefer the per-section structured chunker so
            # studyProgramme.* fields and per-year Valvillkor buckets each
            # become individually-retrievable chunks instead of one 36KB blob.
            structured: list[RetrievedChunk] = []
            if "/student/kurser/program/" in final_url:
                structured = _studyplan_chunks_from_html(
                    html, final_url=final_url, fetched_at=now, lang="sv"
                )
            if structured:
                source_urls.append(final_url)
                chunks.extend(structured)
                log.info(
                    "dynamic-web: emitted %d structured chunks from %s",
                    len(structured),
                    final_url,
                )
                # Cross-cohort fallback: if this is a /arskursN page that
                # didn't yield an eligibility chunk for the requested term,
                # fetch the most recent prior term that has one and surface
                # its block as a labeled, caveated chunk. This handles the
                # case where the läsårsplan for a fresh term hasn't been
                # populated with the "behörighetsgivande kurser" section yet.
                _maybe_emit_fallback_eligibility(
                    html=html,
                    final_url=final_url,
                    structured=structured,
                    cfg=cfg,
                    patterns=patterns,
                    cache=cache,
                    visited=visited,
                    chunks=chunks,
                    source_urls=source_urls,
                )
            else:
                source_urls.append(final_url)
                section = _program_page_section_label(final_url)
                chunks.append(
                    RetrievedChunk(
                        chunk_id=f"web:{final_url}",
                        text=content,
                        rel_source=final_url,
                        doc_title=title,
                        doc_type="html",
                        language="sv",
                        section_path=section,
                        chunk_index=0,
                        chroma_distance=0.0,
                        rerank_score=3.5,
                        source_url=final_url,
                        fetched_at=now,
                        is_stale=False,
                    )
                )
            if "/student/kurser/program/" in final_url:
                for u in _program_links(html, final_url):
                    if len(queue) + len(visited) >= max_pages:
                        break
                    if _is_redundant_sidebar_url(u):
                        continue
                    if u not in visited and _is_allowed_url(u, cfg, patterns):
                        log.info("dynamic-web: discovered linked program page %s", u)
                        queue.append(u)
        except Exception as e:
            log.warning("dynamic web fetch failed for %s: %s", target, e)
            cached = cache.get(target)
            if cached is None:
                log.warning("dynamic-web: no cache available for %s", target)
                return WebFetchResult(failure_url=target)
            age = cache.age_days(cached.fetched_at)
            if age > cfg.dynamic_web.cache_ttl_days:
                log.warning(
                    "dynamic-web: cache for %s is too old (%sd > %sd)",
                    target,
                    age,
                    cfg.dynamic_web.cache_ttl_days,
                )
                return WebFetchResult(failure_url=target)
            used_stale = True
            stale_days = max(stale_days, age)
            log.info("dynamic-web: using cached page %s (age=%sd)", cached.url, age)
            # Stale-cache content is text-only (HTML wasn't preserved), so we
            # can't re-derive structured chunks here. Surface the cached body
            # as a single chunk and rely on the rerank score gradient (2.5 vs
            # the 3.5 used by structured chunks) so fresh structured content
            # outranks stale dumps when both are present.
            source_urls.append(cached.url)
            section = _program_page_section_label(cached.url)
            chunks.append(
                RetrievedChunk(
                    chunk_id=f"web:{cached.url}",
                    text=cached.content,
                    rel_source=cached.url,
                    doc_title=cached.title,
                    doc_type="html",
                    language="sv",
                    section_path=section,
                    chunk_index=0,
                    chroma_distance=0.0,
                    rerank_score=2.5,
                    source_url=cached.url,
                    fetched_at=cached.fetched_at,
                    is_stale=True,
                )
            )

    if not chunks:
        if invalid_course_codes:
            return WebFetchResult(
                missing_kth_course=_bilingual_missing_kth_course_message(invalid_course_codes)
            )
        return None
    return WebFetchResult(
        chunks=chunks,
        source_urls=source_urls,
        used_stale_cache=used_stale,
        stale_age_days=stale_days,
        resolved_program_code=resolved_program_code,
        applied_admission_term=hints.exact_term,
        applied_admission_year_prefix=hints.year_prefix,
    )
