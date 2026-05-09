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
from bs4 import BeautifulSoup, NavigableString, Tag

from student_bot.config import Config, get_config


_HTML_ACCEPT = "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.1"
_NOISY_LINK_TEXT_RE = re.compile(
    r"\b(kontakt|it-support|sök|search|till sidans topp|cookie|integritet|privacy)\b",
    re.I,
)


@dataclass
class UrlSeed:
    url: str
    follow_links: bool = False
    max_depth: int = 0
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    type_hint: str = "auto"
    doc_title_override: str = ""
    # Per-entry override of `cfg.url_ingest.max_pages_per_seed`. None = use global.
    max_pages: int | None = None


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
        include_raw = e.get("include_patterns")
        exclude_raw = e.get("exclude_patterns")
        include_list = include_raw if isinstance(include_raw, list) else []
        exclude_list = exclude_raw if isinstance(exclude_raw, list) else []

        max_pages_raw = e.get("max_pages")
        max_pages = (
            int(max_pages_raw) if isinstance(max_pages_raw, int) and max_pages_raw > 0 else None
        )

        out.append(
            UrlSeed(
                url=_canonicalize_url(str(e["url"])),
                follow_links=bool(e.get("follow_links", False)),
                max_depth=max(0, md),
                include_patterns=[str(x) for x in include_list] or None,
                exclude_patterns=[str(x) for x in exclude_list] or None,
                type_hint=str(e.get("type_hint", "auto")).lower(),
                doc_title_override=str(e.get("doc_title_override", "")).strip(),
                max_pages=max_pages,
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


def _node_text_with_markdown_links(node: Tag, base_url: str, cfg: Config) -> str:
    parts: list[str] = []

    def walk(curr: Tag) -> None:
        for child in curr.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            if child.name == "a" and child.get("href"):
                href = str(child.get("href") or "").strip()
                label = child.get_text(" ", strip=True)
                if not label:
                    continue
                canonical = _canonicalize_url(urljoin(base_url, href))
                blocked = _blocked_link_reason(cfg, canonical)
                if blocked or not _related_link_allowed(cfg, urlsplit(canonical).netloc):
                    parts.append(label)
                else:
                    parts.append(f"[{label}]({canonical})")
            else:
                walk(child)

    walk(node)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _extract_html_markdown(
    payload: bytes, base_url: str, cfg: Config
) -> tuple[str, str, list[str]]:
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
    # H1 is captured into `title` above and re-emitted once by `_render_md`,
    # so skip it here to avoid the page heading appearing twice in output.
    for node in soup.select("h2,h3,p,li,dt,dd"):
        txt = _node_text_with_markdown_links(node, base_url, cfg)
        if not txt:
            continue
        if node.name in ("h2", "h3"):
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
        text = (a.get_text(" ", strip=True) or "").strip()
        if text and _NOISY_LINK_TEXT_RE.search(text):
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
    vetted_links: list[str] | None = None,
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
    out = "\n".join(front) + body_md.strip()
    if vetted_links:
        out += "\n\n## Related links\n"
        out += "\n".join(f"- {u}" for u in vetted_links)
    return out.strip() + "\n"


def _blocked_link_reason(cfg: Config, canonical_url: str) -> str | None:
    host = urlsplit(canonical_url).netloc
    if _host_allowed(host, cfg.url_ingest.domain_global_link_blocklist):
        return f"host:{host.lower()}"
    for pat in cfg.url_ingest.global_link_blocklist_url_patterns:
        if re.search(pat, canonical_url):
            return f"pattern:{pat}"
    return None


def _related_link_allowed(cfg: Config, host: str) -> bool:
    if _host_allowed(host, cfg.url_ingest.domains_ingest_allowlist):
        return True
    return _host_allowed(host, cfg.url_ingest.domains_related_links_allowlist)


def _record_filtered_link(
    report: dict[str, Any],
    reason: str,
    url: str,
    source_rel: str,
    *,
    sample_cap: int = 20,
) -> None:
    report["total_filtered"] = int(report.get("total_filtered", 0)) + 1
    bucket = report.setdefault("reasons", {}).setdefault(reason, {"count": 0, "samples": []})
    bucket["count"] = int(bucket.get("count", 0)) + 1
    if len(bucket["samples"]) < sample_cap:
        bucket["samples"].append({"url": url, "source": source_rel})


def _vetted_links_for_doc(
    cfg: Config,
    links: list[str],
    *,
    source_rel: str,
    filtered_report: dict[str, Any] | None = None,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for u in links:
        c = _canonicalize_url(u)
        blocked_reason = _blocked_link_reason(cfg, c)
        if blocked_reason:
            if filtered_report is not None:
                _record_filtered_link(filtered_report, blocked_reason, c, source_rel)
            continue
        host = urlsplit(c).netloc
        if not _related_link_allowed(cfg, host):
            continue
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
        if len(out) >= max(0, cfg.url_ingest.max_links_per_doc):
            break
    return out


def _write_source_map(path: Path, mapping: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _record_skip_reason(
    stats: dict[str, dict[str, Any]],
    reason: str,
    url: str,
    *,
    sample_cap: int = 5,
) -> None:
    bucket = stats.setdefault(reason, {"count": 0, "samples": []})
    bucket["count"] = int(bucket.get("count", 0)) + 1
    if len(bucket["samples"]) < sample_cap:
        bucket["samples"].append(url)


@click.command()
@click.option("--limit-seeds", type=int, default=None, help="Process at most N manifest entries.")
def main(limit_seeds: int | None) -> None:
    run_started = time.time()
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
    filtered_links_report: dict[str, Any] = {"total_filtered": 0, "reasons": {}}
    written = 0
    skipped = 0
    explicit_seed_count = len(seeds)
    discovered_total = 0
    words_total = 0
    links_extracted_total = 0
    links_kept_total = 0
    skip_reasons: dict[str, dict[str, Any]] = {}

    for seed in seeds:
        seed_blocked_reason = _blocked_link_reason(cfg, seed.url)
        if seed_blocked_reason:
            click.echo(f"skip globally blocked seed: {seed.url} ({seed_blocked_reason})")
            skipped += 1
            _record_skip_reason(
                skip_reasons, f"globally_blocked_seed:{seed_blocked_reason}", seed.url
            )
            continue
        if not _host_allowed(urlsplit(seed.url).netloc, cfg.url_ingest.domains_ingest_allowlist):
            click.echo(f"skip disallowed host: {seed.url}")
            skipped += 1
            _record_skip_reason(skip_reasons, "disallowed_seed_host", seed.url)
            continue
        q: deque[tuple[str, int]] = deque([(seed.url, 0)])
        seen: set[str] = set()
        processed = 0
        seed_cap = seed.max_pages or cfg.url_ingest.max_pages_per_seed

        while q and processed < seed_cap:
            url, depth = q.popleft()
            discovered_total += 1
            is_seed = depth == 0
            url = _canonicalize_url(url)
            if url in seen:
                continue
            seen.add(url)

            blocked_reason = _blocked_link_reason(cfg, url)
            if blocked_reason:
                skipped += 1
                _record_skip_reason(skip_reasons, f"globally_blocked_url:{blocked_reason}", url)
                continue
            if not _host_allowed(urlsplit(url).netloc, cfg.url_ingest.domains_ingest_allowlist):
                skipped += 1
                _record_skip_reason(skip_reasons, "disallowed_host", url)
                continue
            # Apply include/exclude policy only to discovered links.
            # The seed URL itself should always be attempted.
            if (not is_seed) and (not _matches_policy(url, seed)):
                skipped += 1
                _record_skip_reason(skip_reasons, "manifest_policy_filtered", url)
                continue

            try:
                final_url, payload, content_type = _fetch(url, cfg)
            except Exception as e:
                click.echo(f"fetch failed: {url} ({e})")
                skipped += 1
                _record_skip_reason(skip_reasons, "fetch_failed", url)
                continue

            if not _host_allowed(
                urlsplit(final_url).netloc, cfg.url_ingest.domains_ingest_allowlist
            ):
                click.echo(f"skip redirect outside allowlist: {url} -> {final_url}")
                skipped += 1
                _record_skip_reason(skip_reasons, "redirect_outside_allowlist", final_url)
                continue

            hint_pdf = seed.type_hint == "pdf" or final_url.lower().endswith(".pdf")
            is_pdf = hint_pdf or "application/pdf" in content_type
            fetched_at = int(time.time())

            links: list[str] = []
            if is_pdf:
                body_md, title = _extract_pdf_markdown(payload)
                content_kind = "application/pdf"
            else:
                body_md, title, links = _extract_html_markdown(payload, final_url, cfg)
                content_kind = "text/html"
            if seed.doc_title_override:
                title = seed.doc_title_override

            # KTH replaces retired pages with a stub whose title is the
            # literal "PURGED" (the rest of the body is just an "Add to
            # calendar" link). Don't ingest those — they're noise.
            if (title or "").strip() == "PURGED":
                click.echo(f"skip purged page: {final_url}")
                skipped += 1
                _record_skip_reason(skip_reasons, "kth_purged_page", final_url)
                continue

            rel_source = _rel_source_for_url(final_url, output_rel)
            vetted_links: list[str] = []
            if cfg.url_ingest.include_vetted_links_in_markdown and links:
                vetted_links = _vetted_links_for_doc(
                    cfg,
                    links,
                    source_rel=rel_source,
                    filtered_report=filtered_links_report,
                )

            words_total += _word_count(body_md)
            links_extracted_total += len(links)
            links_kept_total += len(vetted_links)

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
                    vetted_links=vetted_links,
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
    filtered_report_path = cfg.absolute(Path(cfg.url_ingest.filtered_links_report_file))
    _write_source_map(filtered_report_path, filtered_links_report)
    click.echo(f"Wrote markdown pages: {written}, skipped: {skipped}")
    click.echo(f"Wrote source map: {source_map_path}")
    if cfg.url_ingest.include_vetted_links_in_markdown:
        click.echo(f"Wrote filtered link report: {filtered_report_path}")
    elapsed_s = max(0.0, time.time() - run_started)
    avg_words = (words_total / written) if written else 0.0
    avg_links_extracted = (links_extracted_total / written) if written else 0.0
    avg_links_kept = (links_kept_total / written) if written else 0.0
    click.echo("Run summary:")
    click.echo(f"  Seeds listed: {explicit_seed_count}")
    click.echo(f"  URLs discovered (seed + followed): {discovered_total}")
    click.echo(f"  Pages written: {written}")
    click.echo(f"  Pages skipped: {skipped}")
    click.echo(f"  Duration: {elapsed_s:.2f}s")
    click.echo(f"  Words total: {words_total} (avg/page: {avg_words:.1f})")
    click.echo(
        f"  Links extracted total: {links_extracted_total} (avg/page: {avg_links_extracted:.1f})"
    )
    if cfg.url_ingest.include_vetted_links_in_markdown:
        click.echo(
            f"  Vetted links kept total: {links_kept_total} (avg/page: {avg_links_kept:.1f})"
        )
    if skip_reasons:
        click.echo("  Skip reasons:")
        for reason in sorted(skip_reasons):
            data = skip_reasons[reason]
            samples = ", ".join(data.get("samples", []))
            if samples:
                click.echo(f"    - {reason}: {data['count']} (samples: {samples})")
            else:
                click.echo(f"    - {reason}: {data['count']}")


if __name__ == "__main__":
    main()
