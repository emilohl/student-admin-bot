"""Audit course-code patterns seen across KTH programme pages (all years).

KTH programme pages render course lists inside ``window.__compressedApplicationStore__``
(URL-encoded JSON), not in server-rendered HTML. This script:

- Seeds programme roots from alias cache plus local ``web_cache`` / ``qa_log``.
- BFS-crawls ``/student/kurser/program/…`` pages.
- From each page, decodes the compressed store and extracts course codes from the
  full JSON (plus visible text and ``<a href>`` to ``/kurs/`` as fallbacks).
- When the store contains ``programmeTerms`` and ``lengthInStudyYears``, enqueues
  ``/{code}/{term}`` and ``/{code}/{term}/arskurs{n}`` so all cohort years are covered.

Requires network access to KTH.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from collections import Counter, deque
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from student_bot.bot.web_retrieval import _COURSE_CODE_RE, _get_program_aliases
from student_bot.config import get_config

KTH_HOST = "www.kth.se"
KTH_SCHEME = "https"
PROGRAM_PATH_RE = re.compile(r"^/student/kurser/program/[A-Z]{5}(?:/.*)?/?$")
_COMP_STORE_RE = re.compile(
    r'window\.__compressedApplicationStore__\s*=\s*"([^"]+)"\s*;',
    re.DOTALL,
)

# Loose candidates: two letters + digits + optional trailing letter(s), excluding HTyyyy/VTyyyy.
_LOOSE_TOKEN_RE = re.compile(r"\b(?!(?:HT|VT)[0-9]{4}\b)([A-Z]{2}[0-9]{2,6}[A-Z]{0,2})\b")


def _canonicalize(url: str) -> str:
    s = urlsplit(url)
    path = re.sub(r"/{2,}", "/", s.path or "/")
    return urlunsplit((KTH_SCHEME, KTH_HOST, path.rstrip("/") or "/", "", ""))


def _fetch_html(url: str, timeout: float, max_bytes: int, user_agent: str) -> tuple[str, str]:
    req = Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
    )
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"response too large for {url}")
        final = _canonicalize(resp.geturl())
    return final, body.decode("utf-8", errors="replace")


def _compressed_application_store(html: str) -> dict | None:
    m = _COMP_STORE_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(urllib.parse.unquote(m.group(1)))
    except (json.JSONDecodeError, ValueError):
        return None


def _program_code_from_url(url: str) -> str | None:
    m = re.search(r"/student/kurser/program/([A-Z]{5})(?:/|$)", url)
    return m.group(1) if m else None


def _normalized_programme_terms(store: dict) -> list[str]:
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
    return out


def _urls_from_programme_manifest(store: dict, request_url: str) -> set[str]:
    """Synthesise cohort + study-year URLs from programme root JSON."""
    code = store.get("programmeCode") or _program_code_from_url(request_url)
    if not code or not re.fullmatch(r"[A-Z]{5}", str(code).upper()):
        return set()
    code = str(code).upper()
    terms = _normalized_programme_terms(store)
    years = int(store.get("lengthInStudyYears") or 0) or 5
    base = f"https://{KTH_HOST}/student/kurser/program/{code}"
    out: set[str] = set()
    for term in terms:
        if not re.fullmatch(r"\d{5}", str(term)):
            continue
        t = str(term)
        out.add(f"{base}/{t}")
        for n in range(1, years + 1):
            out.add(f"{base}/{t}/arskurs{n}")
    return out


def _course_codes_from_store(store: dict) -> set[str]:
    blob = json.dumps(store, ensure_ascii=False).upper()
    return set(_COURSE_CODE_RE.findall(blob))


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return soup.get_text(" ", strip=True)


def _program_links(html: str, base_url: str) -> set[str]:
    soup = BeautifulSoup(html, "lxml")
    out: set[str] = set()
    for a in soup.find_all("a", href=True):
        u = _canonicalize(urljoin(base_url, a["href"]))
        if not u.startswith(f"{KTH_SCHEME}://{KTH_HOST}"):
            continue
        path = urlsplit(u).path.rstrip("/") or "/"
        with_slash = path if path.endswith("/") else f"{path}/"
        if PROGRAM_PATH_RE.match(path) or PROGRAM_PATH_RE.match(with_slash):
            out.add(u.rstrip("/"))
    return out


def _course_codes_from_links(html: str, base_url: str) -> set[str]:
    soup = BeautifulSoup(html, "lxml")
    out: set[str] = set()
    for a in soup.find_all("a", href=True):
        joined = urljoin(base_url, a["href"]).split("#", 1)[0].split("?", 1)[0]
        canon = _canonicalize(joined)
        path = urlsplit(canon).path
        m = re.search(r"/student/kurser/kurs/([^/]+)/?$", path, re.I)
        if m:
            out.add(m.group(1).upper())
    return out


def _classify_code(code: str, matched: Counter, unmatched: Counter) -> None:
    if _COURSE_CODE_RE.fullmatch(code.upper()):
        matched[code.upper()] += 1
    else:
        unmatched[code.upper()] += 1


def main() -> None:
    cfg = get_config()
    timeout = cfg.dynamic_web.timeout_seconds
    max_bytes = cfg.dynamic_web.max_bytes
    user_agent = cfg.dynamic_web.user_agent
    max_program_pages = min(2500, max(200, cfg.dynamic_web.max_links_followed * 100))

    aliases = _get_program_aliases(cfg)
    codes = {v for v in aliases.values() if re.fullmatch(r"[A-Z]{5}", v)}

    cache_db = cfg.absolute(Path(cfg.dynamic_web.cache_db))
    if cache_db.exists():
        with sqlite3.connect(cache_db) as conn:
            for (url,) in conn.execute("SELECT url FROM web_cache"):
                m = re.search(r"/student/kurser/program/([A-Z]{5})(?:/|$)", url or "")
                if m:
                    codes.add(m.group(1))

    logs_db = cfg.absolute(cfg.paths.logs_db)
    if logs_db.exists():
        with sqlite3.connect(logs_db) as conn:
            for (chunk_ids_json,) in conn.execute("SELECT retrieved_chunk_ids FROM qa_log"):
                if not chunk_ids_json:
                    continue
                try:
                    chunk_ids = json.loads(chunk_ids_json)
                except Exception:
                    continue
                for cid in chunk_ids:
                    if not isinstance(cid, str):
                        continue
                    m = re.search(r"/student/kurser/program/([A-Z]{5})(?:/|$)", cid)
                    if m:
                        codes.add(m.group(1))

    seeds = {f"https://{KTH_HOST}/student/kurser/program/{code}" for code in sorted(codes)}
    if not seeds:
        print("No program codes discovered from aliases/cache/logs. Nothing to audit.")
        return

    queue: deque[str] = deque(sorted(seeds))
    queued: set[str] = set(s.rstrip("/") for s in seeds)
    visited: set[str] = set()
    matched = Counter()
    unmatched = Counter()
    errors: list[dict[str, str]] = []
    pages_with_store = 0
    pages_without_store = 0

    def enqueue(u: str) -> None:
        k = u.rstrip("/")
        if k in visited or len(queued) >= max_program_pages * 4:
            return
        if k not in queued:
            queued.add(k)
            queue.append(u)

    while queue and len(visited) < max_program_pages:
        url = queue.popleft()
        req_key = url.rstrip("/")
        if req_key in visited:
            continue
        try:
            final_url, html = _fetch_html(url, timeout, max_bytes, user_agent)
            final_key = final_url.rstrip("/")
            if final_key in visited:
                continue
            visited.add(final_key)

            store = _compressed_application_store(html)
            if store:
                pages_with_store += 1
                for c in _course_codes_from_store(store):
                    matched[c] += 1
                for child in _urls_from_programme_manifest(store, url):
                    enqueue(child)
            else:
                pages_without_store += 1

            for prog_url in _program_links(html, final_url):
                enqueue(prog_url)

            upper_text = _extract_text(html).upper()
            for code in _course_codes_from_links(html, final_url):
                _classify_code(code, matched, unmatched)

            explicit = set(_COURSE_CODE_RE.findall(upper_text))
            for token in explicit:
                matched[token] += 1
            seen_loose = set(explicit)
            for m_l in _LOOSE_TOKEN_RE.finditer(upper_text):
                tok = m_l.group(1)
                if tok in seen_loose:
                    continue
                seen_loose.add(tok)
                if _COURSE_CODE_RE.fullmatch(tok):
                    matched[tok] += 1
                elif (
                    _COURSE_CODE_RE.fullmatch(tok) is None
                    and 5 <= len(tok) <= 10
                    and re.fullmatch(r"[A-Z]{2}[0-9]+(?:[A-Z]+)?", tok)
                ):
                    unmatched[tok] += 1

        except Exception as e:  # pragma: no cover - network/runtime dependent
            errors.append({"url": url, "error": str(e)})
            visited.add(req_key)

    out = {
        "program_urls_visited_unique": len(visited),
        "max_program_pages_cap": max_program_pages,
        "pages_with_compressed_store": pages_with_store,
        "pages_without_compressed_store": pages_without_store,
        "matched_unique": len({k for k in matched}),
        "unmatched_unique": len({k for k in unmatched}),
        "top_unmatched": unmatched.most_common(80),
        "errors": errors[:80],
        "matched_total_observations": int(sum(matched.values())),
        "unmatched_total_observations": int(sum(unmatched.values())),
    }

    out_path = Path("data/course_code_pattern_audit.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"BFS visited unique programme URLs: {len(visited)} (cap {max_program_pages})")
    print(f"Pages with compressed store: {pages_with_store} without: {pages_without_store}")
    print(
        f"Course code hits (matched, all pages): {sum(matched.values())} ({len(set(matched))} unique)"
    )
    print(f"Unmatched heuristic hits: {sum(unmatched.values())} ({len(set(unmatched))} unique)")
    if unmatched:
        print("Top unmatched:")
        for tok, n in unmatched.most_common(20):
            print(f"  {tok}: {n}")
    print(f"Wrote report: {out_path}")


if __name__ == "__main__":
    main()
