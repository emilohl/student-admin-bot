"""Retrieval: query → top-N from Chroma → cross-encoder rerank → top-K."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from sentence_transformers import CrossEncoder

from student_bot.config import Config
from student_bot.ingest.embed import encode_query, get_chroma_collection


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


@lru_cache(maxsize=1)
def _reranker(cfg_key: tuple) -> CrossEncoder:
    model, device = cfg_key
    return CrossEncoder(model, device=device)


def get_reranker(cfg: Config) -> CrossEncoder:
    return _reranker((cfg.reranker.model, cfg.reranker.device))


def retrieve(cfg: Config, query: str) -> RetrievalResult:
    coll = get_chroma_collection(cfg)
    qvec = encode_query(cfg, query).tolist()
    res = coll.query(
        query_embeddings=[qvec],
        n_results=cfg.reranker.candidates,
        include=["documents", "metadatas", "distances"],
    )

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
            )
        )

    if not candidates:
        return RetrievalResult(query=query)

    pairs = [(query, c.text) for c in candidates]
    scores = get_reranker(cfg).predict(pairs).tolist()
    for c, s in zip(candidates, scores):
        c.rerank_score = float(s)

    candidates.sort(key=lambda c: c.rerank_score, reverse=True)
    reranked = candidates[: cfg.reranker.keep]

    return RetrievalResult(query=query, candidates=candidates, reranked=reranked)


__all__ = ["RetrievedChunk", "RetrievalResult", "retrieve", "get_reranker"]
