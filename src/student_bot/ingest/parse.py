"""Document parsing: PDF / Markdown / HTML → plain text with section metadata.

PDF default: pymupdf4llm (markdown output preserves headers reasonably well).
PDF fallback for table-heavy files: docling (only loaded if listed in config).
HTML: BeautifulSoup, drops scripts/styles.
Markdown: pass-through.

Output: ParsedDoc with full text plus a list of (line_index, header_level, header_text)
markers used downstream by the chunker to reconstruct section_path per chunk.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf4llm
from bs4 import BeautifulSoup

from student_bot.config import Config
from student_bot.lang import detect


HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass
class Header:
    line_index: int
    level: int
    text: str


@dataclass
class ParsedDoc:
    source: Path           # absolute path
    rel_source: str        # path relative to docs root (used as stable id)
    doc_title: str
    doc_type: str          # "policy" | "curriculum" | "faq" | "html" | "other"
    language: str          # "sv" | "en"
    text: str              # markdown-ish plain text with header lines preserved
    headers: list[Header] = field(default_factory=list)
    # Page number for each line in `text` (1-indexed for PDFs; None for MD/HTML).
    # Same length as text.splitlines(); used downstream to attach `page_start`
    # to each chunk so we can deep-link citations.
    line_pages: list[int | None] = field(default_factory=list)


def _doc_type_from_path(rel_path: str) -> str:
    # macOS gives NFD strings from the filesystem; normalize to NFC so 'ä'
    # comparisons work no matter where the literal came from.
    p = unicodedata.normalize("NFC", rel_path).lower()
    if "utbildningsplan" in p:
        return "curriculum"
    if "allmänna" in p or "allmanna" in p or "policy" in p or "riktlinje" in p:
        return "policy"
    if "faq" in p:
        return "faq"
    if p.endswith(".html") or p.endswith(".htm"):
        return "html"
    return "other"


def _extract_headers(text: str) -> list[Header]:
    headers: list[Header] = []
    for i, line in enumerate(text.splitlines()):
        m = HEADER_RE.match(line)
        if m:
            headers.append(Header(line_index=i, level=len(m.group(1)), text=m.group(2).strip()))
    return headers


def _parse_pdf(path: Path, use_docling: bool) -> tuple[str, list[int | None]]:
    """Returns (text, line_pages). line_pages is 1-indexed; None lines (e.g.
    page separators we insert) keep alignment with text.splitlines()."""
    if use_docling:
        from docling.document_converter import DocumentConverter  # heavy; lazy
        result = DocumentConverter().convert(str(path))
        text = result.document.export_to_markdown()
        return text, [None] * len(text.splitlines())

    pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
    text_parts: list[str] = []
    line_pages: list[int | None] = []
    for i, p in enumerate(pages):
        page_num = (p.get("metadata") or {}).get("page", i + 1)
        ptext = (p.get("text") or "").strip()
        if not ptext:
            continue
        if text_parts:
            # Blank separator between pages so the chunker treats them as
            # paragraph boundaries.
            text_parts.append("")
            line_pages.append(None)
        for line in ptext.split("\n"):
            text_parts.append(line)
            line_pages.append(page_num)
    return "\n".join(text_parts), line_pages


def _parse_html(path: Path) -> tuple[str, list[int | None]]:
    with path.open("rb") as f:
        soup = BeautifulSoup(f, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    body = soup.get_text("\n", strip=True)
    text = f"# {title}\n\n{body}" if title else body
    return text, [None] * len(text.splitlines())


def _parse_markdown(path: Path) -> tuple[str, list[int | None]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text, [None] * len(text.splitlines())


def parse_file(path: Path, cfg: Config, docs_root: Path) -> ParsedDoc | None:
    """Parse one file. Returns None if format isn't supported."""
    suffix = path.suffix.lower()
    rel = str(path.relative_to(docs_root))

    if suffix == ".pdf":
        use_docling = rel in cfg.ingest.docling_files or path.name in cfg.ingest.docling_files
        text, line_pages = _parse_pdf(path, use_docling)
    elif suffix in (".md", ".markdown"):
        text, line_pages = _parse_markdown(path)
    elif suffix in (".html", ".htm"):
        text, line_pages = _parse_html(path)
    else:
        return None

    if not text.strip():
        return None

    title = path.stem.replace("_", " ").replace("-", " ")
    return ParsedDoc(
        source=path.resolve(),
        rel_source=rel,
        doc_title=title,
        doc_type=_doc_type_from_path(rel),
        language=detect(text[:2000]),
        text=text,
        headers=_extract_headers(text),
        line_pages=line_pages,
    )


def iter_corpus(cfg: Config) -> list[Path]:
    """All ingestable files under the corpus dir, sorted for stable order."""
    root = cfg.absolute(cfg.paths.docs_dir).resolve()
    out: list[Path] = []
    for ext in ("*.pdf", "*.md", "*.markdown", "*.html", "*.htm"):
        out.extend(root.rglob(ext))
    return sorted(out)


__all__ = ["ParsedDoc", "Header", "parse_file", "iter_corpus"]
