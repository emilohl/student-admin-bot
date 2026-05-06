"""Build source links, render the Sources block, and rotate LLM-literacy footers.

Citations are the bot's primary defence against blind trust:
1. Every answer ends with a Sources section listing each cited chunk.
2. Each citation links to the source document so a student can verify.
3. A short rotating footer reinforces a different LLM-literacy concept each
   time, so the lesson isn't ignored as a static disclaimer.
"""

from __future__ import annotations

import random
import re
from urllib.parse import quote

from student_bot.bot.retrieval import RetrievedChunk
from student_bot.config import Config


_INLINE_CITATION_RE = re.compile(r"\[([^\[\]]+?)\]")


def build_doc_url(rel_source: str, page_start: int | None, base_url: str) -> str:
    """Return a URL the user can click to read the source.

    `base_url` is something like "" (no link), "/docs" (web app file mount),
    or "https://kth.example.org/docs" if hosted externally. PDFs get
    `#page=N` so the browser jumps to the cited page.
    """
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
        title, section, page = row
        url = build_doc_url(chunk_by_row[row].rel_source, page, base)
        page_suffix = f", s. {page}" if page else ""
        section_suffix = f" — {section}" if section else ""
        text = f"{title}{section_suffix}{page_suffix}"
        lines.append(f"{i}. [{text}]({url})" if url else f"{i}. {text}")
    return "\n".join(lines)


# Five rotating LLM-literacy reminders, one shown per answer. Sourced from
# the README's "five concepts" list — keep these short so they don't bury
# the answer.
LITERACY_FOOTERS_SV = [
    "_Tips: klicka på källorna och dubbelkolla svaren mot dokumenten — boten kan ha fel även när den låter säker._",
    "_Tips: en stor språkmodell (LLM) kan låta övertygande utan att ha rätt. Lita på källorna, inte på tonen._",
    "_Tips: boten känner bara till dokumenten den indexerats på. För personliga ärenden — kontakta studievägledaren._",
    "_Tips: dina frågor loggas anonymt för att förbättra boten. Skicka `!privacy off` om du vill stänga av loggning._",
    "_Tips: boten är ett komplement, inte en ersättning för studievägledaren — särskilt vid beslut som påverkar dina studier._",
]
LITERACY_FOOTERS_EN = [
    "_Tip: click the sources and double-check against the documents — the bot can be wrong even when it sounds confident._",
    "_Tip: an LLM can sound convincing while being wrong. Trust the sources, not the tone._",
    "_Tip: the bot only knows the documents it was indexed on. For personal cases, contact the study counselor._",
    "_Tip: your questions are logged anonymously to improve the bot. Send `!privacy off` to disable logging._",
    "_Tip: this bot complements but doesn't replace the study counselor — especially for decisions affecting your studies._",
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
        section = c.section_path or ""
        if section:
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

    def _replace(m: re.Match) -> str:
        content = m.group(1).strip()
        idx = by_full.get(content)
        if idx is None:
            idx = by_full.get(content.replace(" · ", " — "))
        if idx is None:
            idx = by_full.get(content.replace(" — ", " · "))
        if idx is None:
            # Title-only fallback when unambiguous.
            sep = content.find(" · ")
            title = (content[:sep] if sep > -1 else content).strip()
            candidates = by_title.get(title, [])
            if len(candidates) == 1:
                idx = candidates[0]
        if idx is None:
            return m.group(0)
        return f"[{_assign(idx)}]"

    new_body = _INLINE_CITATION_RE.sub(_replace, body)
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
        url = build_doc_url(c.rel_source, c.page_start, base)
        page_suffix = f", s. {c.page_start}" if c.page_start else ""
        section_suffix = f" — {c.section_path}" if c.section_path else ""
        field_title = f"{c.doc_title}{section_suffix}{page_suffix}"
        if url:
            link_label = "Visa dokument" if lang == "sv" else "Open document"
            field_value = f"[{link_label}]({url})"
        else:
            field_value = "—"
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
    "format_sources_block",
    "apply_citation_numbering",
    "literacy_footer",
    "confidence_badge",
    "format_for_mattermost",
]
