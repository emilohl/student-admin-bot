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
    "civilingenjorsutbildning",
    "civilingenjorsprogram",
    "civilingenjor",
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
    if "program" in lower or "utbildningsplan" in lower or "study plan" in lower:
        return True
    return _parse_programme_year_level(q or "") is not None


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


def merge_programme_clarification_followup(question: str, history: list[dict] | None) -> str:
    """If the user is answering our admission-year question, fuse with the prior user ask."""
    hist = history or []
    if len(hist) < 2:
        return question
    last = hist[-1]
    if last.get("role") != "assistant":
        return question
    if not is_programme_clarification_assistant_message(last.get("content", "")):
        return question

    qstrip = question.strip()
    hints = parse_program_admission_hints(question)
    bare_year = bool(re.fullmatch(r"20\d{2}", qstrip))
    if not (hints.exact_term or hints.year_prefix or bare_year):
        return question

    prev_user = ""
    for entry in reversed(hist[:-1]):
        if entry.get("role") == "user":
            prev_user = (entry.get("content") or "").strip()
            break
    if not prev_user:
        return question
    merged = f"{prev_user}\n\n{qstrip}"
    log.info("dynamic-web: merged programme clarification follow-up with prior user question")
    return merged


def history_without_programme_clarification_tail(
    history: list[dict], programme_followup_merged: bool
) -> list[dict]:
    """Drop the last user+assistant pair when folded into the current user prompt."""
    if not programme_followup_merged or len(history) < 2 or history[-1].get("role") != "assistant":
        return history
    if not is_programme_clarification_assistant_message(history[-1].get("content", "")):
        return history
    return history[:-2]


@dataclass
class WebFetchResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    used_stale_cache: bool = False
    stale_age_days: int = 0
    failure_url: str = ""
    # Bilingual clarification when cohort (programme period) can't be inferred.
    clarification: tuple[str, str] | None = None
    # KTH may return 200 + empty SPA shell (h1 «undefined …») for non-existent codes.
    missing_kth_course: tuple[str, str] | None = None
    missing_kth_program: tuple[str, str] | None = None


def _compiled_patterns(cfg: Config) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in cfg.dynamic_web.allowed_patterns]


def _canonicalize(url: str) -> str:
    """Normalize KTH URLs — host/scheme, collapse path slashes, strip query and fragment."""
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
    """True when KTH returns the empty course SPA (h1 is repeated «undefined»)."""
    raw = (title or "").strip()
    if not raw:
        return False
    tokens = re.split(r"\s+", raw.lower())
    return len(tokens) >= 3 and all(tok == "undefined" for tok in tokens)


def _programme_root_title_is_unknown_code_shell(title: str) -> bool:
    """True when the HTML `<title>` is only «CODE (CODE), Utbildningsplaner» (unknown programme).

    KTH serves HTTP 200 for invented codes; the visible «utbildningsplan saknas» text is
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
        f"KTH:s kurssidor listar ingen kurs med koden {tail} — sidan är bara en tom "
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
        f"KTH:s programkatalog listar ingen utbildning med koden {tail} — sidan är bara "
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


def _extract_targets(question: str) -> list[str]:
    raise RuntimeError("_extract_targets requires cfg; use _extract_targets_with_cfg")


def _extract_targets_with_cfg(question: str, cfg: Config) -> list[str]:
    urls: list[str] = []
    for code in _COURSE_CODE_RE.findall(question.upper()):
        urls.append(f"https://{_KTH_HOST}/student/kurser/kurs/{code}")

    # Program lookup is triggered by explicit program intent words OR explicit
    # study-year phrasing (e.g. "år 2", "second year").
    lower = question.lower()
    if (
        "program" in lower
        or "utbildningsplan" in lower
        or "study plan" in lower
        or _parse_programme_year_level(question) is not None
    ):
        aliases = _get_program_aliases(cfg)
        known_codes = {v for v in aliases.values() if re.fullmatch(r"[A-Z]{5}", v)}
        # Only accept explicit program codes when the user wrote them in code form
        # (uppercase token, e.g. CTFYS). Avoid interpreting lowercase words like
        # "fysik" as a code.
        for code in _PROGRAM_CODE_RE.findall(question):
            # Fallback: when alias/code snapshot is empty (e.g. fetch outage), still
            # probe explicit uppercase code tokens and validate by fetching KTH root.
            if not known_codes:
                log.warning(
                    "dynamic-web: programme code list empty; "
                    "probing bare token %s directly via KTH root page",
                    code,
                )
                urls.append(f"https://{_KTH_HOST}/student/kurser/program/{code}")
                continue
            if code not in known_codes:
                log.info("dynamic-web: ignoring unknown program-like token %s", code)
                continue
            urls.append(f"https://{_KTH_HOST}/student/kurser/program/{code}")
        qn = _norm(question)
        q_tokens = set(re.findall(r"[a-z0-9åäö]+", qn))

        matched_aliases: list[tuple[str, str]] = []

        def alias_match(alias: str) -> bool:
            if not alias:
                return False
            alias_tokens = set(re.findall(r"[a-z0-9åäö]+", alias))
            strong = {t for t in alias_tokens if len(t) >= 4 and t not in _GENERIC_ALIAS_TOKENS}
            if alias == qn:
                return True
            if alias in qn:
                # Avoid matching broad labels like "masterprogram" or single
                # subject words like "fysik" in multi-word questions.
                if len(strong) < 2:
                    return False
                return strong.issubset(q_tokens)
            # Also allow semantic token overlap when the full alias phrase isn't
            # present verbatim (e.g. "masterprogrammet i teknisk fysik" should
            # hit alias "civilingenjörsutbildning i teknisk fysik").
            if len(strong) >= 2 and len(strong.intersection(q_tokens)) >= 2:
                return True
            return False

        # Prefer longest aliases first so "teknisk fysik" wins over "fysik".
        for alias in sorted(aliases.keys(), key=len, reverse=True):
            if alias_match(alias):
                code = aliases[alias]
                if re.fullmatch(r"[A-Z]{5}", code):
                    matched_aliases.append((alias, code))
                    urls.append(f"https://{_KTH_HOST}/student/kurser/program/{code}")
        if matched_aliases:
            log.info("dynamic-web: matched program aliases=%s", matched_aliases)
    # Stable dedupe
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
    "VV": "Valbara kurslistor — villkorligt valbara (VV)",
    "K": "Konditionsvalfria kurser (K)",
    "KV": "Konditionsvalfria kurser (KV)",
    "VK": "Valbara kurslistor — villkorligt valbara (VK)",
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
    """Swedish-style hp string, e.g. 4 -> «4,0 hp», 7.5 -> «7,5 hp»."""
    return f"{float(n):.1f}".replace(".", ",") + " hp"


def _credits_suffix_sv(c: dict) -> str:
    """Return « (7,5 hp)» from omfattning / credits fields, or ``\"\"`` if unknown."""
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
        return f"- **{cc}** — {name_s}{hp}"
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
                    lines.append(f"- **{cc}** — {name_s}{hp}" if name_s else f"- **{cc}**{hp}")
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
                    "eng. ungefär «conditionally elective» — val inom godkända listor enligt planen). "
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


def _clarify_program_terms_sv(program_code: str, terms: list[str]) -> str:
    span = _intake_year_bounds_from_terms(terms)
    span_line = ""
    if span:
        y0, y1 = span
        if y0 == y1:
            span_line = f"\nJust nu finns webbdata för ungefär **{y0}** som startår."
        else:
            span_line = f"\nJust nu finns webbdata för år mellan **{y0}** och **{y1}**."
    return (
        f"För att visa rätt utbildningsplan för **{program_code}** behöver jag veta "
        "vilken antagningsomgång som gäller. Skriv gärna t.ex. **HT2024** eller **VT2025**."
        f"{span_line}"
    )


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
    return (
        f"To show the right study plan for **{program_code}**, which **admission round** applies to you? "
        "Please mention e.g. **autumn intake (HT2024)** or **spring (VT2025)**."
        f"{span_line}"
    )


def _select_programme_urls(
    program_code: str,
    sorted_terms_desc: list[str],
    hints: AdmissionHints,
    year_level: int | None = None,
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

    resolved = _select_programme_urls(code, terms, hints, year_level=year_level)
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
    """For /program/<CODE>/<TERM>(/arskursN), return a stable study-plan bundle:
    base term page + year pages + common sidebar pages."""
    path = urlsplit(_canonicalize(url)).path
    m = _PROGRAM_TERM_RE.fullmatch(path)
    if not m:
        return [_canonicalize(url)]
    code, term, _year = m.group(1).upper(), m.group(2), m.group(3)
    base = _canonicalize(f"https://{_KTH_HOST}/student/kurser/program/{code}/{term}")
    out: list[str] = [base]
    out.extend([f"{base}/arskurs{n}" for n in range(1, 6)])
    out.extend([f"{base}/{slug}" for slug in _PROGRAM_SIDEBAR_SLUGS])
    return _dedupe_urls(out)


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


def maybe_fetch_dynamic_web(
    cfg: Config,
    question: str,
    _lang: str = "sv",
) -> WebFetchResult | None:
    if not cfg.dynamic_web.enabled:
        return None

    unknown_codes = _explicit_unknown_programme_codes(question, cfg)
    if unknown_codes:
        return WebFetchResult(
            missing_kth_program=_bilingual_missing_kth_program_message(unknown_codes),
        )

    patterns = _compiled_patterns(cfg)
    targets = _extract_targets_with_cfg(question, cfg)
    if not targets:
        return None
    log.info("dynamic-web: targets=%s", targets)

    course_urls = [t for t in targets if "/student/kurser/kurs/" in t]
    prog_roots = [t for t in targets if _is_program_root_only_url(t)]
    hints = parse_program_admission_hints(question)
    year_level = _parse_programme_year_level(question)

    queue: list[str] = list(course_urls)
    for root in prog_roots:
        res = _resolve_program_root_targets(cfg, root, hints, year_level=year_level)
        if res.missing_program_codes:
            return WebFetchResult(
                missing_kth_program=_bilingual_missing_kth_program_message(
                    list(res.missing_program_codes),
                ),
            )
        if res.clarification_sv:
            return WebFetchResult(
                clarification=(res.clarification_sv, res.clarification_en),
            )
        for u in res.queue_urls:
            queue.extend(_programme_term_bundle_urls(u))

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
    )
