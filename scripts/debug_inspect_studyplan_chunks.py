"""Ad-hoc: dump the structured chunks the dynamic-web path would emit for
CTFYS HT2023, so we can eyeball size, content, and where the budget goes.

Run: `uv run python scripts/inspect_studyplan_chunks.py`
"""

from __future__ import annotations

from student_bot.bot.web_retrieval import (
    _fetch_html,
    _programme_term_bundle_urls,
    _studyplan_chunks_from_html,
    _truncate_web_chunk_text,
    _programme_page_text_with_store,
    _sanitize_to_text,
)
from student_bot.config import get_config

cfg = get_config()

PROGRAM_TERM_URL = "https://www.kth.se/student/kurser/program/CTFYS/20232"

urls = _programme_term_bundle_urls(PROGRAM_TERM_URL)
print(f"Bundle for CTFYS HT2023 has {len(urls)} URLs:")
for u in urls:
    print(f"  {u}")
print()

# Approximate 4 chars per token (matches pipeline._estimate_tokens).
def est_tokens(s: str) -> int:
    return max(0, len(s or "") // 4)


totals_structured = 0
totals_legacy = 0
per_page_summary = []

for url in urls:
    try:
        final_url, html = _fetch_html(url, cfg)
    except Exception as e:
        print(f"FETCH FAILED {url}: {e}")
        continue

    title, visible = _sanitize_to_text(html)
    if "/student/kurser/program/" in final_url:
        legacy_content = _programme_page_text_with_store(html, visible, final_url)
    else:
        legacy_content = visible
    legacy_content = _truncate_web_chunk_text(legacy_content)
    legacy_chars = len(legacy_content)
    legacy_tokens = est_tokens(legacy_content)

    structured = _studyplan_chunks_from_html(
        html, final_url=final_url, fetched_at=0, lang="sv"
    )

    page_struct_chars = sum(len(c.text) for c in structured)
    page_struct_tokens = est_tokens("".join(c.text for c in structured))
    totals_structured += page_struct_tokens
    totals_legacy += legacy_tokens
    per_page_summary.append(
        (final_url, len(structured), page_struct_chars, page_struct_tokens, legacy_chars, legacy_tokens)
    )

    print(f"\n=== {final_url} ===")
    print(f"  title: {title!r}")
    print(f"  structured chunks: {len(structured)}  total {page_struct_chars} chars (~{page_struct_tokens} tok)")
    print(f"  legacy single-blob: {legacy_chars} chars (~{legacy_tokens} tok)")
    for i, c in enumerate(structured, 1):
        section = (c.section_path or "").strip() or "-"
        preview = c.text.replace("\n", " / ").strip()
        if len(preview) > 220:
            preview = preview[:220] + "…"
        print(
            f"   [{i:2}] {len(c.text):5d} ch  rerank={c.rerank_score:.2f}  "
            f"section={section!r}"
        )
        print(f"        {preview}")

print("\n=================================================")
print("Per-page summary  (chunks, struct_chars, struct_tok, legacy_chars, legacy_tok)")
for u, n, sc, st, lc, lt in per_page_summary:
    short = u.replace("https://www.kth.se/student/kurser/program/", "")
    print(f"  {short:40} n={n:3d}  struct={sc:6d}ch/{st:5d}tok  legacy={lc:6d}ch/{lt:5d}tok")
print(f"\nTOTAL structured tokens across bundle: ~{totals_structured}")
print(f"TOTAL legacy single-blob tokens:       ~{totals_legacy}")
print(f"LLM num_ctx budget:                    {cfg.llm.num_ctx}")
