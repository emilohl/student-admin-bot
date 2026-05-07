"""Fetch URL/PDF corpus entries from a manifest and write markdown files.

Per-entry policies in `data/url_manifest.yaml` control whether linked pages are
followed and how deep the crawl goes.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import click
import pymupdf4llm
import yaml
from bs4 import BeautifulSoup

from student_bot.config import Config, get_config


_HTML_ACCEPT = "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.1"


@dataclass
class UrlSeed:
    url: str
    follow_links: bool = False
    max_depth: int = 0
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    type_hint: str = "auto"
    doc_title_override: str = ""


def _canonicalize_url(url: str) -> str:
    s = urlsplit(url.strip())
    scheme = (s.scheme or "https").lower()
    host = s.netloc.lower()
    path = re.sub(r"/{2,}", "/", s.path or "/").rstrip("/") or "/"
    return urlunsplit((scheme, host, path, "", ""))


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    h = (host or "").lower()
    for allowed in allowlist:
        a = allowed.lower().lstrip(".")
        if h == a or h.endswith("." + a):
            return True
    return False


def _load_manifest(path: Path, cfg: Config) -> list[UrlSeed]:
    if not path.exists():
        raise click.ClickException(f"manifest does not exist: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("entries", raw if isinstance(raw, list) else [])
    out: list[UrlSeed] = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("url"):
            continue
        md = int(e.get("max_depth", cfg.url_ingest.default_max_depth))
        out.append(
            UrlSeed(
                url=_canonicalize_url(str(e["url"])),
                follow_links=bool(e.get("follow_links", False)),
                max_depth=max(0, md),
                include_patterns=[str(x) for x in e.get("include_patterns", [])] or None,
                exclude_patterns=[str(x) for x in e.get("exclude_patterns", [])] or None,
                type_hint=str(e.get("type_hint", "auto")).lower(),
                doc_title_override=str(e.get("doc_title_override", "")).strip(),
            )
        )
    return out


def _matches_policy(url: str, seed: UrlSeed) -> bool:
    path = urlsplit(url).path or "/"
    if seed.include_patterns:
        if not any(re.search(p, path) for p in seed.include_patterns):
            return False
    if seed.exclude_patterns:
        if any(re.search(p, path) for p in seed.exclude_patterns):
            return False
    return True


def _fetch(url: str, cfg: Config) -> tuple[str, bytes, str]:
    req = Request(
        url,
        headers={
            "User-Agent": cfg.dynamic_web.user_agent,
            "Accept": _HTML_ACCEPT,
        },
    )
    with urlopen(req, timeout=cfg.url_ingest.timeout_seconds) as resp:
        final_url = _canonicalize_url(resp.geturl())
        payload = resp.read(cfg.url_ingest.max_bytes + 1)
        if len(payload) > cfg.url_ingest.max_bytes:
            raise ValueError("response exceeded max_bytes")
        content_type = (resp.headers.get("Content-Type") or "").lower()
    return final_url, payload, content_type


def _extract_html_markdown(payload: bytes, base_url: str) -> tuple[str, str, list[str]]:
    soup = BeautifulSoup(payload, "lxml")
    for t in soup(["script", "style", "noscript", "form", "header", "footer", "nav"]):
        t.decompose()
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True) or title

    lines: list[str] = []
    for node in soup.select("h1,h2,h3,p,li,dt,dd"):
        txt = node.get_text(" ", strip=True)
        if not txt:
            continue
        if node.name in ("h1", "h2", "h3"):
            lines.append(f"{'#' * int(node.name[1])} {txt}")
        else:
            lines.append(txt)
    # Flatten simple table rows to improve downstream chunk semantics.
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        cells = [c for c in cells if c]
        if cells:
            lines.append(" | ".join(cells))

    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        if not href:
            continue
        links.append(_canonicalize_url(urljoin(base_url, href)))

    body = "\n\n".join(lines).strip()
    md = (f"# {title}\n\n{body}" if title else body).strip()
    return md, title or "Web source", links


def _extract_pdf_markdown(payload: bytes) -> tuple[str, str]:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(payload)
        tmp.flush()
        md = pymupdf4llm.to_markdown(tmp.name)
    lines = [ln.strip() for ln in md.splitlines() if ln.strip()]
    title = lines[0].lstrip("# ").strip() if lines else "PDF source"
    return md.strip(), title


def _safe_slug(text: str) -> str:
    t = re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-").lower()
    return t[:70] or "page"


def _rel_source_for_url(canonical_url: str, output_dir: Path) -> str:
    s = urlsplit(canonical_url)
    base = _safe_slug(Path(s.path).stem or "root")
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:10]
    rel = output_dir / s.netloc.lower() / f"{base}-{digest}.md"
    return str(rel).replace("\\", "/")


def _render_md(
    *,
    source_url: str,
    canonical_url: str,
    fetched_at: int,
    title: str,
    content_type: str,
    body_md: str,
) -> str:
    front = [
        "---",
        f"source_url: {source_url}",
        f"canonical_url: {canonical_url}",
        f"fetched_at: {fetched_at}",
        f"title: {title}",
        f"content_type: {content_type or 'unknown'}",
        "---",
        "",
    ]
    return "\n".join(front) + body_md.strip() + "\n"


def _write_source_map(path: Path, mapping: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


@click.command()
@click.option("--limit-seeds", type=int, default=None, help="Process at most N manifest entries.")
def main(limit_seeds: int | None) -> None:
    cfg = get_config()
    if not cfg.url_ingest.enabled:
        raise click.ClickException("url_ingest.enabled is false in config.yaml")

    manifest_path = cfg.absolute(Path(cfg.url_ingest.manifest_file))
    seeds = _load_manifest(manifest_path, cfg)
    if limit_seeds:
        seeds = seeds[:limit_seeds]
    if not seeds:
        click.echo("No manifest entries found.")
        return

    docs_root = cfg.absolute(cfg.paths.docs_dir).resolve()
    output_dir_abs = cfg.absolute(Path(cfg.url_ingest.output_dir)).resolve()
    try:
        output_rel = output_dir_abs.relative_to(docs_root)
    except ValueError as e:
        raise click.ClickException(
            f"url_ingest.output_dir must be inside docs_dir ({docs_root})"
        ) from e
    output_dir_abs.mkdir(parents=True, exist_ok=True)

    source_map: dict[str, dict[str, Any]] = {}
    written = 0
    skipped = 0

    for seed in seeds:
        if not _host_allowed(urlsplit(seed.url).netloc, cfg.url_ingest.domains_allowlist):
            click.echo(f"skip disallowed host: {seed.url}")
            skipped += 1
            continue
        q: deque[tuple[str, int]] = deque([(seed.url, 0)])
        seen: set[str] = set()
        processed = 0

        while q and processed < cfg.url_ingest.max_pages_per_seed:
            url, depth = q.popleft()
            url = _canonicalize_url(url)
            if url in seen:
                continue
            seen.add(url)

            if not _host_allowed(urlsplit(url).netloc, cfg.url_ingest.domains_allowlist):
                continue
            if not _matches_policy(url, seed):
                continue

            try:
                final_url, payload, content_type = _fetch(url, cfg)
            except Exception as e:
                click.echo(f"fetch failed: {url} ({e})")
                skipped += 1
                continue

            if not _host_allowed(urlsplit(final_url).netloc, cfg.url_ingest.domains_allowlist):
                click.echo(f"skip redirect outside allowlist: {url} -> {final_url}")
                skipped += 1
                continue

            hint_pdf = seed.type_hint == "pdf" or final_url.lower().endswith(".pdf")
            is_pdf = hint_pdf or "application/pdf" in content_type
            fetched_at = int(time.time())

            links: list[str] = []
            if is_pdf:
                body_md, title = _extract_pdf_markdown(payload)
                content_kind = "application/pdf"
            else:
                body_md, title, links = _extract_html_markdown(payload, final_url)
                content_kind = "text/html"
            if seed.doc_title_override:
                title = seed.doc_title_override

            rel_source = _rel_source_for_url(final_url, output_rel)
            out_path = docs_root / rel_source
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                _render_md(
                    source_url=url,
                    canonical_url=final_url,
                    fetched_at=fetched_at,
                    title=title,
                    content_type=content_kind,
                    body_md=body_md,
                ),
                encoding="utf-8",
            )
            source_map[rel_source] = {
                "source_url": url,
                "canonical_url": final_url,
                "fetched_at": fetched_at,
                "title": title,
                "content_type": content_kind,
            }
            written += 1
            processed += 1

            if seed.follow_links and depth < seed.max_depth:
                for link in links:
                    if link not in seen and _matches_policy(link, seed):
                        q.append((link, depth + 1))

    source_map_path = cfg.absolute(Path(cfg.url_ingest.source_map_file))
    _write_source_map(source_map_path, source_map)
    click.echo(f"Wrote markdown pages: {written}, skipped: {skipped}")
    click.echo(f"Wrote source map: {source_map_path}")


if __name__ == "__main__":
    main()
