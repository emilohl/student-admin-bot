"""End-to-end token-usage measurement for representative CTFYS HT2023 queries
before vs after the rerank/dedup changes. Calls maybe_fetch_dynamic_web and
the pipeline's web-chunk reranker, measuring chunks-in vs chunks-out and the
prompt-token estimate going to the LLM.
"""

from __future__ import annotations

from student_bot.bot.pipeline import _rerank_web_chunks, _estimate_tokens
from student_bot.bot.prompts import compose_messages
from student_bot.bot.web_retrieval import maybe_fetch_dynamic_web
from student_bot.config import get_config

cfg = get_config()

QUERIES = [
    "Vilka valfria kurser är listade för årskurs 3 för CTFYS HT2023?",
    "Vilka masterprogram kan jag välja mellan på CTFYS HT2023?",
    "Vad är betygsskalan på CTFYS-programmet HT2023?",
    "Hur stor är CTFYS HT2023 och vilken examen ger det?",
]


def measure(query: str) -> None:
    print(f"\n=== {query!r} ===")
    web = maybe_fetch_dynamic_web(cfg, query, "sv")
    if not web or not web.chunks:
        print("  (no web result)")
        return

    raw_chunks = list(web.chunks)
    raw_chars = sum(len(c.text) for c in raw_chunks)
    raw_tokens = _estimate_tokens("".join(c.text for c in raw_chunks))

    # Mirror pipeline.answer() behaviour (rerank + cap to top-K).
    reranked = _rerank_web_chunks(cfg, query, "sv", list(raw_chunks))
    rr_chars = sum(len(c.text) for c in reranked)
    rr_tokens = _estimate_tokens("".join(c.text for c in reranked))

    # Build the user message both ways to see real prompt-token deltas.
    msgs_before = compose_messages(cfg, "sv", [], raw_chunks, query)
    msgs_after = compose_messages(cfg, "sv", [], reranked, query)
    pt_before = sum(_estimate_tokens(m.get("content", "")) for m in msgs_before)
    pt_after = sum(_estimate_tokens(m.get("content", "")) for m in msgs_after)

    print(
        f"  chunks: {len(raw_chunks)} -> {len(reranked)}  "
        f"(chars {raw_chars} -> {rr_chars}; chunk tokens {raw_tokens} -> {rr_tokens})"
    )
    print(
        f"  full prompt tokens: {pt_before} -> {pt_after}  "
        f"(save {pt_before - pt_after}, ctx limit {cfg.llm.num_ctx})"
    )
    print(f"  top-{cfg.reranker.keep} sections (rerank logit, chars):")
    for c in reranked:
        sec = (c.section_path or "-").strip()
        sec = sec[:78] + "…" if len(sec) > 78 else sec
        print(f"    {c.rerank_score:7.3f}  {len(c.text):5d} ch  {sec}")


for q in QUERIES:
    measure(q)
