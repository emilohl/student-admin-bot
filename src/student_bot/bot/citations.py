"""Build source links, render the Sources block, and rotate LLM-literacy footers.

Citations are the bot's primary defence against blind trust:
1. Every answer ends with a Sources section listing each cited chunk.
2. Each citation links to the source document so a student can verify.
3. A short rotating footer reinforces a different LLM-literacy concept each
   time, so the lesson isn't ignored as a static disclaimer.
"""
from __future__ import annotations

import random
from urllib.parse import quote

from student_bot.bot.retrieval import RetrievedChunk
from student_bot.config import Config


def build_doc_url(rel_source: str, page_start: int | None, base_url: str) -> str:
    """Return a URL the user can click to read the source.

    `base_url` is something like "" (no link), "/docs" (web app file mount),
    or "https://kth.example.org/docs" if hosted externally. PDFs get
    `#page=N` so the browser jumps to the cited page.
    """
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
    "_Tips: klicka på källorna och dubbelkolla mot dokumenten — boten kan ha fel även när den låter säker._",
    "_Tips: ett LLM kan låta övertygande utan att ha rätt. Lita på källorna, inte på tonen._",
    "_Tips: boten känner bara till dokumenten den indexerats på. Personliga ärenden — kontakta studievägledaren._",
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


def confidence_badge(lang: str, top1: float) -> str:
    """Tiny one-word confidence label derived from the gate's top1 score."""
    if top1 >= 3.0:
        return "Hög" if lang == "sv" else "High"
    if top1 >= 0.5:
        return "Medel" if lang == "sv" else "Medium"
    return "Låg" if lang == "sv" else "Low"


__all__ = [
    "build_doc_url",
    "format_sources_block",
    "literacy_footer",
    "confidence_badge",
]
