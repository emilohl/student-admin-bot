"""Render curated local markdown files (with optional YAML frontmatter).

Used by the `/doc/<rel_source>` route to display authored content (FAQ,
Information, etc.) as styled HTML instead of raw markdown text. Files under
`docs/corpus/web_import/` are deliberately *not* rendered here — those have
upstream `source_url` values and should be linked to the original page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from markdown_it import MarkdownIt


_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class Author:
    name: str
    role: str = ""


@dataclass
class RenderedDoc:
    title: str
    body_html: str
    authors: list[Author]
    updated: str = ""


def _coerce_authors(fm: dict) -> list[Author]:
    """Accept either a list under `authors` (entries can be strings or
    `{name, role}` dicts) or a single `author` + optional `role` pair."""
    raw = fm.get("authors")
    out: list[Author] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    out.append(Author(name=name))
            elif isinstance(item, dict):
                name = str(item.get("name", "") or "").strip()
                if not name:
                    continue
                out.append(Author(name=name, role=str(item.get("role", "") or "").strip()))
        if out:
            return out
    name = str(fm.get("author", "") or "").strip()
    if name:
        out.append(Author(name=name, role=str(fm.get("role", "") or "").strip()))
    return out


def _split_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, text[m.end() :]


def _make_renderer() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "breaks": False, "linkify": False})
    md.enable(["table", "strikethrough"])
    return md


_RENDERER = _make_renderer()


def render_file(path: Path) -> RenderedDoc:
    """Read `path` and return a RenderedDoc. Frontmatter is optional."""
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)

    title = str(fm.get("title", "") or "").strip()
    if not title:
        m = _H1_RE.search(body)
        if m:
            title = m.group(1).strip()
    if not title:
        title = path.stem

    return RenderedDoc(
        title=title,
        body_html=_RENDERER.render(body),
        authors=_coerce_authors(fm),
        updated=str(fm.get("updated", "") or "").strip(),
    )


__all__ = ["Author", "RenderedDoc", "render_file"]
