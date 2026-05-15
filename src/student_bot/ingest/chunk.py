"""Token-aware recursive splitter.

Greedy: walks the document line by line, accumulating tokens until target,
then emits a chunk with overlap. Header lines snapshot the current section_path
so each chunk knows where it lives. Splits on paragraph (\\n\\n) and sentence
boundaries when individual blocks exceed the target.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable

from student_bot.config import Config
from student_bot.ingest.parse import HEADER_RE, ParsedDoc


SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÅÄÖ])")


@dataclass
class Chunk:
    chunk_id: str  # stable: f"{rel_source}#{idx}"
    rel_source: str
    doc_title: str
    doc_type: str
    language: str
    section_path: list[str]  # outer→inner header texts
    chunk_index: int
    text: str  # plain text, no leading/trailing whitespace
    content_hash: str  # sha256 of the text for incremental indexing
    page_start: int | None = None  # PDF page (1-indexed) or None
    metadata: dict = field(default_factory=dict)


def _split_long_block(block: str, max_tokens: int, count: Callable[[str], int]) -> list[str]:
    """Split an over-long paragraph at sentence boundaries; fall back to hard slice."""
    if count(block) <= max_tokens:
        return [block]
    sentences = SENTENCE_RE.split(block)
    pieces: list[str] = []
    cur: list[str] = []
    cur_tokens = 0
    for sent in sentences:
        st = count(sent)
        if cur and cur_tokens + st > max_tokens:
            pieces.append(" ".join(cur))
            cur = [sent]
            cur_tokens = st
        else:
            cur.append(sent)
            cur_tokens += st
    if cur:
        pieces.append(" ".join(cur))

    # Anything still over-budget gets hard-sliced on whitespace tokens.
    final: list[str] = []
    for p in pieces:
        if count(p) <= max_tokens:
            final.append(p)
            continue
        words = p.split()
        sub: list[str] = []
        sub_tokens = 0
        for w in words:
            wt = count(w + " ")
            if sub and sub_tokens + wt > max_tokens:
                final.append(" ".join(sub))
                sub = [w]
                sub_tokens = wt
            else:
                sub.append(w)
                sub_tokens += wt
        if sub:
            final.append(" ".join(sub))
    return final


def _section_path(stack: list[tuple[int, str]]) -> list[str]:
    return [text for _, text in stack]


def chunk_document(
    doc: ParsedDoc,
    cfg: Config,
    token_count: Callable[[str], int],
) -> list[Chunk]:
    target = cfg.ingest.chunk.target_tokens
    overlap = cfg.ingest.chunk.overlap_tokens

    def page_for_line(line_idx: int) -> int | None:
        if 0 <= line_idx < len(doc.line_pages):
            return doc.line_pages[line_idx]
        return None

    # Build a stream of (section_path, text_block, line_start) from the doc.
    blocks: list[tuple[list[str], str, int]] = []
    header_stack: list[tuple[int, str]] = []
    paragraph_buf: list[str] = []
    paragraph_start_line: int = -1

    def flush_paragraph():
        nonlocal paragraph_start_line
        if paragraph_buf:
            block = "\n".join(paragraph_buf).strip()
            if block:
                blocks.append((_section_path(header_stack), block, paragraph_start_line))
            paragraph_buf.clear()
        paragraph_start_line = -1

    # Only H1–H3 drive section_path (and therefore chunk boundaries). H4+
    # stay as inline content lines so sibling sub-sections — e.g. SCI's
    # per-programme contact cards under "Programansvariga för
    # civilingenjörsprogrammen" — pack together under the H3 parent rather
    # than fragmenting into one tiny chunk per sub-heading. The embedder and
    # cross-encoder still see each sub-heading because it's part of the body.
    _SECTION_BREAK_MAX_LEVEL = 3
    for line_idx, line in enumerate(doc.text.splitlines()):
        m = HEADER_RE.match(line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            if level <= _SECTION_BREAK_MAX_LEVEL:
                flush_paragraph()
                while header_stack and header_stack[-1][0] >= level:
                    header_stack.pop()
                header_stack.append((level, text))
            else:
                if not paragraph_buf:
                    paragraph_start_line = line_idx
                paragraph_buf.append(line)
        elif not line.strip():
            flush_paragraph()
        else:
            if not paragraph_buf:
                paragraph_start_line = line_idx
            paragraph_buf.append(line)
    flush_paragraph()

    # Greedy pack into chunks. Each accumulated piece carries its source
    # line so we can attach a `page_start` to the emitted chunk.
    chunks: list[Chunk] = []
    cur_pieces: list[tuple[str, int]] = []  # (text, line_start)
    cur_tokens = 0
    cur_section: list[str] = []
    idx = 0

    def emit(section: list[str], pieces: list[tuple[str, int]]):
        nonlocal idx
        body = "\n\n".join(p[0] for p in pieces).strip()
        if not body:
            return
        # Prepend the section path as a header line so it's part of the text
        # the embedder and cross-encoder see. Without this, tiny chunks (e.g.
        # one contact card under a deep heading) lose at retrieval time — the
        # chunk body says "Christian Ohm · chohm@kth.se" while the section
        # path "… > Programansvariga … > Teknisk fysik" holds the only signal
        # that ties the person to the question. The LLM already receives
        # section info via the citation tag in `format_context`, so this is
        # purely a retrieval-quality fix.
        header = " > ".join(p for p in section if p).strip()
        text = f"{header}\n\n{body}" if header else body
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        page_start = page_for_line(pieces[0][1])
        chunks.append(
            Chunk(
                chunk_id=f"{doc.rel_source}#{idx}",
                rel_source=doc.rel_source,
                doc_title=doc.doc_title,
                doc_type=doc.doc_type,
                language=doc.language,
                section_path=section,
                chunk_index=idx,
                text=text,
                content_hash=h,
                page_start=page_start,
            )
        )
        idx += 1

    for section_path, block, block_line_start in blocks:
        if cur_pieces and section_path != cur_section:
            emit(cur_section, cur_pieces)
            cur_pieces = []
            cur_tokens = 0

        cur_section = section_path
        for piece in _split_long_block(block, target, token_count):
            pt = token_count(piece)
            if cur_tokens + pt > target and cur_pieces:
                emit(cur_section, cur_pieces)
                tail_text, tail_line = cur_pieces[-1]
                tail_tokens = token_count(tail_text)
                if tail_tokens <= overlap:
                    cur_pieces = [(tail_text, tail_line)]
                    cur_tokens = tail_tokens
                else:
                    words = tail_text.split()
                    keep_tokens = 0
                    keep: list[str] = []
                    for w in reversed(words):
                        wt = token_count(w + " ")
                        if keep_tokens + wt > overlap:
                            break
                        keep.insert(0, w)
                        keep_tokens += wt
                    if keep:
                        cur_pieces = [(" ".join(keep), tail_line)]
                        cur_tokens = keep_tokens
                    else:
                        cur_pieces = []
                        cur_tokens = 0
            cur_pieces.append((piece, block_line_start))
            cur_tokens += pt

    if cur_pieces:
        emit(cur_section, cur_pieces)

    return chunks


__all__ = ["Chunk", "chunk_document"]
