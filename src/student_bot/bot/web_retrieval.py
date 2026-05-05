from __future__ import annotations

import logging
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from student_bot.bot.retrieval import RetrievedChunk
from student_bot.bot.web_cache import CachedPage, WebCache
from student_bot.config import Config

log = logging.getLogger("student_bot")

_KTH_HOST = "www.kth.se"
_KTH_SCHEME = "https"
# Exclude term markers like HT2024 / VT2025 (not course codes).
_COURSE_CODE_RE = re.compile(r"\b(?!(?:HT|VT)[0-9]{4}\b)([A-Z]{2}[0-9]{4})\b")
_PROGRAM_CODE_RE = re.compile(r"\b([A-Z]{5})\b")
_PROGRAM_LIST_EN = "https://www.kth.se/student/kurser/kurser-inom-program?l=en"
_PROGRAM_LIST_SV = "https://www.kth.se/student/kurser/kurser-inom-program"
_PROGRAM_URL_CODE_RE = re.compile(r"/student/kurser/program/([A-Z]{5})(?:/|$)")
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
    "civilingenjor",
    "kth",
    "the",
    "and",
    "for",
    "of",
    "in",
    "i",
}


@dataclass
class WebFetchResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    used_stale_cache: bool = False
    stale_age_days: int = 0
    failure_url: str = ""


def _compiled_patterns(cfg: Config) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in cfg.dynamic_web.allowed_patterns]


def _canonicalize(url: str) -> str:
    s = urlsplit(url)
    path = re.sub(r"/{2,}", "/", s.path or "/")
    return urlunsplit((_KTH_SCHEME, _KTH_HOST, path.rstrip("/") or "/", "", ""))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


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
    return aliases


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

    # Only treat as program lookup when the query mentions "program" or "utbildningsplan".
    lower = question.lower()
    if "program" in lower or "utbildningsplan" in lower or "study plan" in lower:
        aliases = _get_program_aliases(cfg)
        known_codes = {v for v in aliases.values() if re.fullmatch(r"[A-Z]{5}", v)}
        # Only accept explicit program codes when the user wrote them in code form
        # (uppercase token, e.g. CTFYS). Avoid interpreting lowercase words like
        # "fysik" as a code.
        for code in _PROGRAM_CODE_RE.findall(question):
            # If we have an alias/code index, only accept known codes.
            if known_codes and code not in known_codes:
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
    return title or "KTH page", "\n".join(lines)


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


def maybe_fetch_dynamic_web(cfg: Config, question: str) -> WebFetchResult | None:
    if not cfg.dynamic_web.enabled:
        return None

    patterns = _compiled_patterns(cfg)
    targets = _extract_targets_with_cfg(question, cfg)
    if not targets:
        return None
    log.info("dynamic-web: targets=%s", targets)

    cache = WebCache(cfg)
    chunks: list[RetrievedChunk] = []
    source_urls: list[str] = []
    used_stale = False
    stale_days = 0
    max_pages = max(1, min(cfg.dynamic_web.max_pages_per_query, cfg.dynamic_web.max_links_followed))
    queue = list(targets)[:max_pages]
    visited: set[str] = set()

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
            # Validate program fetches: if a requested program code maps to a page
            # whose visible heading mentions another code, treat it as mismatch.
            req_m = _PROGRAM_URL_CODE_RE.search(target)
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
            chunks.append(
                RetrievedChunk(
                    chunk_id=f"web:{final_url}",
                    text=content,
                    rel_source=final_url,
                    doc_title=title,
                    doc_type="html",
                    language="sv",
                    section_path="KTH web",
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
            chunks.append(
                RetrievedChunk(
                    chunk_id=f"web:{cached.url}",
                    text=cached.content,
                    rel_source=cached.url,
                    doc_title=cached.title,
                    doc_type="html",
                    language="sv",
                    section_path="KTH web (cached)",
                    chunk_index=0,
                    chroma_distance=0.0,
                    rerank_score=2.5,
                    source_url=cached.url,
                    fetched_at=cached.fetched_at,
                    is_stale=True,
                )
            )

    if not chunks:
        return None
    return WebFetchResult(
        chunks=chunks,
        source_urls=source_urls,
        used_stale_cache=used_stale,
        stale_age_days=stale_days,
    )
