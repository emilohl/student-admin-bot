"""Build source links, render the Sources block, and rotate LLM-literacy footers.

Citations are the bot's primary defence against blind trust:
1. Every answer ends with a Sources section listing each cited chunk.
2. Each citation links to the source document so a student can verify.
3. A short rotating footer reinforces a different LLM-literacy concept each
   time, so the lesson isn't ignored as a static disclaimer.
"""

from __future__ import annotations

import json
import random
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlparse

from student_bot.bot.retrieval import RetrievedChunk
from student_bot.config import Config


_INLINE_CITATION_RE = re.compile(r"\[([^\[\]]+?)\]")
# Allows one level of nested parens so `(FAQ · Section (KEX-jobb))` captures
# the full inner text. Used as a fallback when the LLM wrote `(...)` instead
# of `[...]` despite the prompt; matches are gated on confident lookups so we
# don't rewrite ordinary parentheticals.
_PARENS_CITATION_RE = re.compile(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)")


def build_doc_url(
    rel_source: str,
    page_start: int | None,
    base_url: str,
    source_url: str = "",
) -> str:
    """Return a URL the user can click to read the source.

    `base_url` is something like "" (no link), "/docs" (web app file mount),
    or "https://kth.example.org/docs" if hosted externally. PDFs get
    `#page=N` so the browser jumps to the cited page.
    """
    if source_url.startswith("https://") or source_url.startswith("http://"):
        return source_url
    if rel_source.startswith("https://") or rel_source.startswith("http://"):
        return rel_source
    if not base_url:
        return ""
    encoded = quote(rel_source, safe="/")
    url = f"{base_url.rstrip('/')}/{encoded}"
    if page_start and rel_source.lower().endswith(".pdf"):
        url += f"#page={page_start}"
    return url


def _dedupe_keep_order(items: list[tuple]) -> list[tuple]:
    seen: set[tuple] = set()
    out: list[tuple] = []
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


@lru_cache(maxsize=8)
def _load_source_map(path_str: str, mtime_ns: int) -> dict[str, dict]:
    # mtime_ns participates in the cache key so edits are picked up without
    # a restart.
    try:
        raw = json.loads(Path(path_str).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for rel, meta in raw.items():
        if isinstance(rel, str) and isinstance(meta, dict):
            out[rel] = meta
    return out


def _source_map(cfg: Config) -> dict[str, dict]:
    path = cfg.absolute(Path(cfg.url_ingest.source_map_file))
    if not path.exists():
        return {}
    return _load_source_map(str(path), path.stat().st_mtime_ns)


def format_source_title(cfg: Config, c: RetrievedChunk) -> str:
    """Human-friendly source title. For web-imported docs prefer
    '<host>: <page title>' from url_source_map metadata."""
    rel = (c.rel_source or "").strip()
    title = (c.doc_title or "").strip()
    if not rel.startswith("web_import/"):
        return title

    meta = _source_map(cfg).get(rel, {})
    pretty = str(meta.get("title", "")).strip()
    source_url = (
        c.source_url or str(meta.get("canonical_url", "")) or str(meta.get("source_url", ""))
    ).strip()
    host = ""
    if source_url.startswith("https://") or source_url.startswith("http://"):
        host = urlparse(source_url).netloc

    if pretty:
        return f"{host}: {pretty}" if host else pretty
    return title


def _http_chunk(c: RetrievedChunk) -> bool:
    u = (c.source_url or c.rel_source or "").strip()
    return u.startswith("http://") or u.startswith("https://")


def format_source_short_title(cfg: Config, c: RetrievedChunk) -> str:
    """Compact title for long KTH web programme headings (footer / SSE meta).

    Prefer "PROG · last title clause" so repeated document boilerplate is not
    re-listed on every reference row."""
    base = format_source_title(cfg, c)
    if not _http_chunk(c):
        return base
    t = (c.doc_title or base or "").strip()
    if not t:
        return base
    if "|" in t:
        t = t.split("|", 1)[0].strip()
    u = (c.source_url or c.rel_source or "").strip()
    code = None
    m = re.search(r"\(([A-Z]{5})\)", t)
    if m:
        code = m.group(1)
    if not code and "/program/" in u:
        m2 = re.search(r"/program/([A-Z]{5})/", u)
        if m2:
            code = m2.group(1)
    tail = t.rsplit(",", 1)[-1].strip() if "," in t else t
    if code and tail and tail != t:
        return f"{code} · {tail}"
    if len(t) > 72 and tail:
        return tail
    return base


def format_source_display_label(cfg: Config, c: RetrievedChunk) -> str:
    """Single-line label for Sources blocks, web UI, and Mattermost fields."""
    primary = format_source_short_title(cfg, c)
    if getattr(c, "is_stale", False):
        primary = f"{primary} (cache)"
    section_part = (c.section_path or "").strip()
    if section_part and section_part.lower() not in primary.lower():
        section_suffix = f" – {section_part}"
    else:
        section_suffix = ""
    page_suffix = f", s. {c.page_start}" if c.page_start else ""
    return f"{primary}{section_suffix}{page_suffix}"


def format_sources_block(
    cfg: Config,
    chunks: list[RetrievedChunk],
    lang: str,
) -> str:
    """Render a markdown 'Sources' block. Empty string if no chunks."""
    if not chunks:
        return ""
    base = cfg.web.doc_base_url
    rows: list[tuple[str, str | None, int | None]] = []
    for c in chunks:
        rows.append((c.doc_title, c.section_path or None, c.page_start))
    rows = _dedupe_keep_order(rows)
    # Map back to a representative chunk per row (first match) to build URL.
    chunk_by_row: dict[tuple, RetrievedChunk] = {}
    for c in chunks:
        key = (c.doc_title, c.section_path or None, c.page_start)
        chunk_by_row.setdefault(key, c)

    label = "Källor" if lang == "sv" else "Sources"
    lines = [f"\n\n**{label}:**"]
    for i, row in enumerate(rows, 1):
        chunk = chunk_by_row[row]
        text = format_source_display_label(cfg, chunk)
        url = build_doc_url(chunk.rel_source, chunk.page_start, base, source_url=chunk.source_url)
        lines.append(f"{i}. [{text}]({url})" if url else f"{i}. {text}")
    return "\n".join(lines)


# Five rotating LLM-literacy reminders, one shown per answer. Sourced from
# the README's "five concepts" list — keep these short so they don't bury
# the answer.
LITERACY_FOOTERS_SV = [
    "_Tips: klicka på källorna och dubbelkolla svaren mot dokumenten – boten kan ha fel även när den låter säker._",
    "_Tips: en stor språkmodell (LLM) kan låta övertygande utan att ha rätt. Lita på källorna, inte på tonen._",
    "_Tips: boten känner bara till dokumenten den indexerats på. För personliga ärenden – kontakta studievägledaren._",
    "_Tips: dina frågor loggas anonymt för att förbättra boten. Skicka `!privacy off` om du vill stänga av loggning._",
    "_Tips: boten är ett komplement, inte en ersättning för studievägledaren – särskilt vid beslut som påverkar dina studier._",
]
LITERACY_FOOTERS_EN = [
    "_Tip: click the sources and double-check against the documents – the bot can be wrong even when it sounds confident._",
    "_Tip: an LLM can sound convincing while being wrong. Trust the sources, not the tone._",
    "_Tip: the bot only knows the documents it was indexed on. For personal cases, contact the study counselor._",
    "_Tip: your questions are logged anonymously to improve the bot. Send `!privacy off` to disable logging._",
    "_Tip: this bot complements but doesn't replace the study counselor – especially for decisions affecting your studies._",
]


def literacy_footer(lang: str, *, seed: int | None = None) -> str:
    pool = LITERACY_FOOTERS_EN if lang == "en" else LITERACY_FOOTERS_SV
    rng = random.Random(seed) if seed is not None else random
    return rng.choice(pool)


def apply_citation_numbering(
    body: str,
    chunks: list[RetrievedChunk],
) -> tuple[str, list[RetrievedChunk]]:
    """Replace inline ``[Title · Section]`` citations in ``body`` with
    ``[N]`` markers numbered in citation order, and return only the
    chunks the model actually cited (one per (title, section, page)
    dedup row, in citation order). Citations that don't match any
    retrieved chunk are left in the body untouched.

    Numbering happens server-side so every channel — Mattermost, CLI,
    and the web UI — gets the same compact reference list and inline
    `[N]` markers, instead of leaving filter+renumber logic to the
    web client to repeat.
    """
    if not chunks:
        return body, []

    # Dedupe by the same key format_sources_block uses, so each Sources
    # row maps to exactly one citation number.
    rows: list[RetrievedChunk] = []
    seen: dict[tuple, int] = {}
    for c in chunks:
        key = (c.doc_title, c.section_path or None, c.page_start)
        if key not in seen:
            seen[key] = len(rows)
            rows.append(c)

    by_full: dict[str, int] = {}
    by_title: dict[str, list[int]] = {}
    for i, c in enumerate(rows):
        title = c.doc_title
        section = (c.section_path or "").strip()
        if section:
            by_full[f"{title} – {section}"] = i
            by_full[f"{title} — {section}"] = i
            by_full[f"{title} · {section}"] = i
        by_title.setdefault(title, []).append(i)

    cited_indices: list[int] = []
    number_for: dict[int, int] = {}

    def _assign(idx: int) -> int:
        n = number_for.get(idx)
        if n is None:
            n = len(cited_indices) + 1
            number_for[idx] = n
            cited_indices.append(idx)
        return n

    def _match(content: str, *, allow_title_only: bool) -> int | None:
        idx = by_full.get(content)
        if idx is not None:
            return idx
        # Try every cross-translation between the three accepted separators
        # (`·`, em-dash `—`, en-dash `–`). The LLM can emit any of them.
        for src, dst in ((" · ", " — "), (" — ", " · "), (" · ", " – "), (" – ", " · ")):
            idx = by_full.get(content.replace(src, dst))
            if idx is not None:
                return idx
        # Longest-prefix match: handles `[Title · Section · Extra]` when the
        # LLM appends invented segments to a registered Title+Section.
        parts = re.split(r"\s+[·—–]\s+", content)
        for k in range(len(parts) - 1, 0, -1):
            prefix = " · ".join(parts[:k])
            idx = (
                by_full.get(prefix)
                or by_full.get(prefix.replace(" · ", " — "))
                or by_full.get(prefix.replace(" · ", " – "))
            )
            if idx is not None:
                return idx
        if not allow_title_only:
            return None
        sep = content.find(" · ")
        title = (content[:sep] if sep > -1 else content).strip()
        candidates = by_title.get(title, [])
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _replace(m: re.Match) -> str:
        content = m.group(1).strip()
        idx = _match(content, allow_title_only=True)
        if idx is None:
            return m.group(0)
        return f"[{_assign(idx)}]"

    def _replace_parens(m: re.Match) -> str:
        content = m.group(1).strip()
        # Require a citation-shaped separator so we never rewrite ordinary
        # parentheticals like "(t.ex. ...)" or "(KEX-jobb)".
        if " · " not in content and " — " not in content and " – " not in content:
            return m.group(0)
        idx = _match(content, allow_title_only=False)
        if idx is None:
            return m.group(0)
        return f"[{_assign(idx)}]"

    new_body = _INLINE_CITATION_RE.sub(_replace, body)
    new_body = _PARENS_CITATION_RE.sub(_replace_parens, new_body)
    cited = [rows[i] for i in cited_indices]
    return new_body, cited


def confidence_badge(lang: str, top1: float) -> str:
    """Tiny one-word confidence label derived from the gate's top1 score."""
    if top1 >= 3.0:
        return "Hög" if lang == "sv" else "High"
    if top1 >= 0.5:
        return "Medel" if lang == "sv" else "Medium"
    return "Låg" if lang == "sv" else "Low"


def _confidence_color(top1: float) -> str:
    """Map confidence_badge thresholds to MM attachment colors. Same buckets
    so the colored sidebar can't disagree with the textual badge."""
    if top1 >= 3.0:
        return "good"
    if top1 >= 0.5:
        return "warning"
    return "danger"


def _truncate_field_value(s: str, limit: int = 1000) -> str:
    """MM attachment field values cap at ~1024 chars. Sources are short, but
    truncate defensively in case a long URL or section path slips in."""
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def format_for_mattermost(cfg, result) -> tuple[str, list[dict] | None]:
    """Build a (message, attachments) pair for posting to Mattermost.

    Returns ``(message, None)`` for paths without sources (gate refusal,
    rate limit, too-long input, empty body) so the caller falls back to
    plain Markdown — `result.rendered` is the right thing to post then.

    On the answered path, the message holds only the (optional) jargon
    transparency note and the body with `[N]` markers; the colored sidebar,
    confidence text, source list, and rotating literacy tip move into a
    single attachment. Pedagogical surfaces required by the README's
    "five concepts" section are all preserved — they just move from inline
    Markdown into structured fields.
    """
    if not result.answered or not result.cited_chunks:
        return result.rendered, None

    lang = result.lang
    body = result.numbered_body or result.answer

    # Rebuild the jargon transparency note the same way pipeline.answer() does
    # so the prefix wording matches exactly.
    jargon_note = ""
    if cfg.jargon.show_transparency_note and result.jargon_hits:
        from student_bot.jargon import Jargon

        jargon = Jargon.from_config(cfg)
        jargon_note = jargon.transparency_note(result.jargon_hits, lang)

    message = (jargon_note + "\n\n" + body if jargon_note else body).strip()

    label = "Tillförlitlighet" if lang == "sv" else "Confidence"
    title = f"{label}: {confidence_badge(lang, result.gate.top1)}"

    base = cfg.web.doc_base_url
    seen: set[tuple] = set()
    fields: list[dict] = []
    for c in result.cited_chunks:
        key = (c.doc_title, c.section_path or None, c.page_start)
        if key in seen:
            continue
        seen.add(key)
        url = build_doc_url(c.rel_source, c.page_start, base, source_url=c.source_url)
        field_title = format_source_display_label(cfg, c)
        if url:
            link_label = "Visa dokument" if lang == "sv" else "Open document"
            field_value = f"[{link_label}]({url})"
        else:
            field_value = "–"
        fields.append(
            {
                "title": _truncate_field_value(field_title, 200),
                "value": _truncate_field_value(field_value),
                "short": True,
            }
        )

    attachment = {
        "color": _confidence_color(result.gate.top1),
        "title": title,
        "fields": fields,
        "footer": literacy_footer(lang),
    }
    return message, [attachment]


__all__ = [
    "build_doc_url",
    "format_source_title",
    "format_source_short_title",
    "format_source_display_label",
    "format_sources_block",
    "apply_citation_numbering",
    "literacy_footer",
    "confidence_badge",
    "format_for_mattermost",
]
