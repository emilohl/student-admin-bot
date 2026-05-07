from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts.fetch_url_corpus import (
    UrlSeed,
    _canonicalize_url,
    _host_allowed,
    _load_manifest,
    _matches_policy,
    _rel_source_for_url,
    _render_md,
)
from student_bot.bot.citations import build_doc_url, format_sources_block
from student_bot.bot.retrieval import RetrievedChunk
from student_bot.config import get_config
from student_bot.ingest.embed import _source_url_map


def test_canonicalize_url_strips_query_fragment_and_normalizes():
    got = _canonicalize_url("HTTPS://WWW.KTH.SE//student/kurser/program/CTFYS/?x=1#foo")
    assert got == "https://www.kth.se/student/kurser/program/CTFYS"


def test_host_allowlist_supports_subdomains():
    assert _host_allowed("www.kth.se", ["kth.se"])
    assert _host_allowed("api.www.kth.se", ["kth.se"])
    assert not _host_allowed("example.org", ["kth.se"])


def test_matches_policy_include_exclude():
    seed = UrlSeed(
        url="https://www.kth.se/x",
        include_patterns=[r"/student/kurser/program/"],
        exclude_patterns=[r"/arskurs5$"],
    )
    assert _matches_policy("https://www.kth.se/student/kurser/program/CTFYS/20252/arskurs1", seed)
    assert not _matches_policy(
        "https://www.kth.se/student/kurser/program/CTFYS/20252/arskurs5", seed
    )


def test_load_manifest_reads_per_url_policy(tmp_path: Path):
    manifest = {
        "entries": [
            {
                "url": "https://www.kth.se/student/kurser/program/CTMAT",
                "follow_links": True,
                "max_depth": 2,
                "include_patterns": ["/student/kurser/program/CTMAT/"],
                "exclude_patterns": ["/arskurs5$"],
                "type_hint": "auto",
                "doc_title_override": "CTMAT policy",
            }
        ]
    }
    mpath = tmp_path / "url_manifest.yaml"
    mpath.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    cfg = get_config()
    out = _load_manifest(mpath, cfg)
    assert len(out) == 1
    seed = out[0]
    assert seed.follow_links is True
    assert seed.max_depth == 2
    assert seed.doc_title_override == "CTMAT policy"


def test_rel_source_for_url_is_stable():
    rel = _rel_source_for_url(
        "https://www.kth.se/student/kurser/program/CTFYS/20252", Path("web_import")
    )
    assert rel.startswith("web_import/www.kth.se/")
    assert rel.endswith(".md")


def test_render_md_contains_source_frontmatter():
    md = _render_md(
        source_url="https://www.kth.se/a",
        canonical_url="https://www.kth.se/b",
        fetched_at=123,
        title="Title",
        content_type="text/html",
        body_md="# Body",
    )
    assert "source_url: https://www.kth.se/a" in md
    assert "canonical_url: https://www.kth.se/b" in md
    assert md.strip().endswith("# Body")


def test_source_url_map_prefers_canonical_url(tmp_path: Path):
    p = tmp_path / "url_source_map.json"
    p.write_text(
        json.dumps({"web_import/a.md": {"source_url": "https://x", "canonical_url": "https://y"}}),
        encoding="utf-8",
    )
    cfg = get_config()
    old = cfg.url_ingest.source_map_file
    cfg.url_ingest.source_map_file = str(p)
    try:
        out = _source_url_map(cfg)
        assert out["web_import/a.md"] == "https://y"
    finally:
        cfg.url_ingest.source_map_file = old


def test_build_doc_url_prefers_source_url():
    got = build_doc_url("docs/policy.pdf", 3, "/docs", source_url="https://www.kth.se/original")
    assert got == "https://www.kth.se/original"


def test_format_sources_block_uses_chunk_source_url():
    cfg = get_config()
    chunks = [
        RetrievedChunk(
            chunk_id="x#0",
            text="...",
            rel_source="web_import/x.md",
            source_url="https://www.kth.se/student/kurser/program/CTFYS/20252/arskurs1",
            doc_title="CTFYS arskurs 1",
            doc_type="html",
            language="sv",
            section_path="KTH web",
            chunk_index=0,
            chroma_distance=0.0,
            page_start=None,
        )
    ]
    md = format_sources_block(cfg, chunks, "sv")
    assert "https://www.kth.se/student/kurser/program/CTFYS/20252/arskurs1" in md
