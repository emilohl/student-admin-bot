"""Compare chunks emitted from /omfattning vs /genomforande for CTFYS HT2023.
Hypothesis: the SPA serves the same __compressedApplicationStore__ JSON to both
routes, and the chunker walks that JSON rather than the rendered DOM — so it
emits the same chunks regardless of which sidebar sub-page was fetched.
"""

from __future__ import annotations
import hashlib

from student_bot.bot.web_retrieval import (
    _fetch_html,
    _studyplan_chunks_from_html,
    _compressed_application_store,
)
from student_bot.config import get_config

cfg = get_config()
URLS = [
    "https://www.kth.se/student/kurser/program/CTFYS/20232/omfattning",
    "https://www.kth.se/student/kurser/program/CTFYS/20232/genomforande",
]


def store_hash(html: str) -> str:
    store = _compressed_application_store(html)
    if store is None:
        return "no-store"
    # Stable hash over the studyProgramme subtree only (the field the chunker reads).
    import json
    sp = store.get("studyProgramme", {})
    return hashlib.sha1(
        json.dumps(sp, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]


data = {}
for url in URLS:
    final_url, html = _fetch_html(url, cfg)
    chunks = _studyplan_chunks_from_html(html, final_url=final_url, fetched_at=0, lang="sv")
    data[url] = {
        "store_hash": store_hash(html),
        "html_size": len(html),
        "chunks": chunks,
    }

print(f"\n{'URL':80} store_hash  html_kb  n_chunks")
for url, d in data.items():
    short = url.replace("https://www.kth.se", "")
    print(f"{short:80} {d['store_hash']}  {len(d['chunks']):3d}        {d['html_size']//1024} kB")

print("\nSection paths emitted (sorted, deduped):")
sections = {url: sorted({c.section_path for c in d["chunks"]}) for url, d in data.items()}
all_sections = sorted(set().union(*sections.values()))
header = ["section_path", *[u.split("/")[-1] for u in URLS]]
print(f"\n{header[0]:80} {header[1]:>12} {header[2]:>12}")
for s in all_sections:
    flags = [("✓" if s in sections[u] else "·") for u in URLS]
    print(f"{s:80} {flags[0]:>12} {flags[1]:>12}")

# Are the chunk *texts* identical, in order?
o = data[URLS[0]]["chunks"]
g = data[URLS[1]]["chunks"]
matched_pairs = 0
for c1 in o:
    for c2 in g:
        if c1.section_path == c2.section_path and c1.text == c2.text:
            matched_pairs += 1
            break
print(f"\nByte-identical (section_path, text) pairs: {matched_pairs} of {len(o)} from /omfattning matched in /genomforande")
