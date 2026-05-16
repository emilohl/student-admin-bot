"""Retrieval: query → top-N from Chroma → cross-encoder rerank → top-K."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import lru_cache

from sentence_transformers import CrossEncoder

from student_bot.config import Config
from student_bot.ingest.embed import encode_query, get_chroma_collection

_CORPUS_FILTER_MIN_PRIMARY = 6


@dataclass
class RetrievedChunk:
    chunk_id: str
    text: str
    rel_source: str
    doc_title: str
    doc_type: str
    language: str
    section_path: str
    chunk_index: int
    chroma_distance: float  # cosine distance from Chroma (lower = closer)
    rerank_score: float = 0.0  # cross-encoder logit
    page_start: int | None = None
    source_url: str = ""
    fetched_at: int | None = None
    is_stale: bool = False


@dataclass
class RetrievalResult:
    query: str
    candidates: list[RetrievedChunk] = field(default_factory=list)  # full top-N from Chroma
    reranked: list[RetrievedChunk] = field(default_factory=list)  # top-K after rerank
    # Wall-clock breakdown of this retrieval, in milliseconds. `chroma_ms`
    # covers the CPU side: query embedding plus the Chroma similarity
    # lookup. `rerank_ms` covers the cross-encoder pass over top-N. Both
    # are None when retrieval was skipped (e.g. a web-fetch-only turn that
    # never touched Chroma).
    chroma_ms: int | None = None
    rerank_ms: int | None = None


@lru_cache(maxsize=1)
def _reranker(cfg_key: tuple) -> CrossEncoder:
    model, device = cfg_key
    return CrossEncoder(model, device=device)


def get_reranker(cfg: Config) -> CrossEncoder:
    return _reranker((cfg.reranker.model, cfg.reranker.device))


def _chunk_matches_programme_hints(chunk: RetrievedChunk, hints: frozenset[str]) -> bool:
    blob = f"{chunk.rel_source} {chunk.chunk_id}".upper()
    return any(part.upper() in blob for part in hints)


def retrieve(
    cfg: Config,
    query: str,
    corpus_programme_substrings: frozenset[str] | None = None,
    query_language: str | None = None,
) -> RetrievalResult:
    coll = get_chroma_collection(cfg)
    # Embedding runs on CPU (bge-m3) and is typically the slower half of
    # the "dense retrieval" bucket — fold it into chroma_ms so the
    # diagnostics panel reports one number for the CPU encode + lookup.
    chroma_t0 = time.monotonic()
    qvec = encode_query(cfg, query).tolist()
    res = coll.query(
        query_embeddings=[qvec],
        n_results=cfg.reranker.candidates,
        include=["documents", "metadatas", "distances"],
    )
    chroma_ms = int((time.monotonic() - chroma_t0) * 1000)

    ids = res["ids"][0] if res.get("ids") else []
    docs = res["documents"][0] if res.get("documents") else []
    metas = res["metadatas"][0] if res.get("metadatas") else []
    dists = res["distances"][0] if res.get("distances") else []

    candidates: list[RetrievedChunk] = []
    for cid, text, meta, d in zip(ids, docs, metas, dists):
        page_raw = meta.get("page_start", 0)
        page = int(page_raw) if page_raw else None
        candidates.append(
            RetrievedChunk(
                chunk_id=cid,
                text=text or "",
                rel_source=meta.get("rel_source", ""),
                doc_title=meta.get("doc_title", ""),
                doc_type=meta.get("doc_type", ""),
                language=meta.get("language", ""),
                section_path=meta.get("section_path", ""),
                chunk_index=int(meta.get("chunk_index", 0)),
                chroma_distance=float(d),
                page_start=page,
                source_url=meta.get("source_url", ""),
            )
        )

    if corpus_programme_substrings:
        narrowed = [
            c for c in candidates if _chunk_matches_programme_hints(c, corpus_programme_substrings)
        ]
        # Prefer cohort-matching study-plan docs without starving rerank input.
        if len(narrowed) >= _CORPUS_FILTER_MIN_PRIMARY:
            candidates = narrowed
        elif narrowed:
            seen_ids = {c.chunk_id for c in narrowed}
            candidates = narrowed + [c for c in candidates if c.chunk_id not in seen_ids]
            candidates = candidates[: cfg.reranker.candidates]

    if not candidates:
        return RetrievalResult(query=query, chroma_ms=chroma_ms)

    rerank_t0 = time.monotonic()
    pairs = [(query, c.text) for c in candidates]
    scores = get_reranker(cfg).predict(pairs).tolist()
    rerank_ms = int((time.monotonic() - rerank_t0) * 1000)
    for c, s in zip(candidates, scores):
        c.rerank_score = float(s)

    # Soft language preference. Adds `language_bonus` to chunks tagged with
    # the same language as the query before the final sort, so parallel SV/EN
    # sources that the reranker scores near each other resolve to the user's
    # language. Chunks with no language tag (older ingest, web-fetched paths)
    # are not penalised — they just don't get the bonus.
    if query_language and cfg.reranker.language_bonus:
        for c in candidates:
            if c.language and c.language == query_language:
                c.rerank_score += cfg.reranker.language_bonus

    candidates.sort(key=lambda c: c.rerank_score, reverse=True)
    reranked = candidates[: cfg.reranker.keep]

    return RetrievalResult(
        query=query,
        candidates=candidates,
        reranked=reranked,
        chroma_ms=chroma_ms,
        rerank_ms=rerank_ms,
    )


__all__ = ["RetrievedChunk", "RetrievalResult", "retrieve", "get_reranker"]
