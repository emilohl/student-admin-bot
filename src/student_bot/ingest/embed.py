"""Embedding + Chroma upsert.

The e5 family REQUIRES specific prefixes:
  passages:  "passage: <text>"
  queries:   "query: <text>"
Without them retrieval quality drops silently. Centralised here so callers
just pass plain text.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Iterable

import chromadb
import numpy as np
from chromadb.config import Settings as ChromaSettings
from sentence_transformers import SentenceTransformer

from student_bot.config import Config
from student_bot.ingest.chunk import Chunk


COLLECTION_NAME = "kth_kb"


@lru_cache(maxsize=1)
def _embedder(cfg_key: tuple) -> SentenceTransformer:
    model, device = cfg_key
    return SentenceTransformer(model, device=device)


def get_embedder(cfg: Config) -> SentenceTransformer:
    return _embedder((cfg.embedding.model, cfg.embedding.device))


def token_count_fn(cfg: Config):
    """Returns a fast token counter using the embedder's tokenizer."""
    tok = get_embedder(cfg).tokenizer

    def count(text: str) -> int:
        if not text:
            return 0
        return len(tok.encode(text, add_special_tokens=False))

    return count


def encode_passages(cfg: Config, texts: list[str]) -> np.ndarray:
    prefixed = [cfg.embedding.passage_prefix + t for t in texts]
    return get_embedder(cfg).encode(
        prefixed,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
        batch_size=16,
    )


def encode_query(cfg: Config, text: str) -> np.ndarray:
    return get_embedder(cfg).encode(
        cfg.embedding.query_prefix + text,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )


def get_chroma_collection(cfg: Config):
    persist_dir = cfg.absolute(cfg.paths.chroma_dir)
    persist_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(persist_dir),
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
    )
    # Cosine on normalised vectors == dot product == proper similarity.
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def existing_hashes(collection) -> dict[str, str]:
    """Return {chunk_id: content_hash} for what's already in Chroma."""
    out: dict[str, str] = {}
    res = collection.get(include=["metadatas"])
    for cid, meta in zip(res.get("ids", []), res.get("metadatas", [])):
        if meta and "content_hash" in meta:
            out[cid] = meta["content_hash"]
    return out


def upsert_chunks(cfg: Config, chunks: Iterable[Chunk], batch_size: int = 64) -> int:
    """Embed and upsert. Skips chunks whose content_hash already matches."""
    collection = get_chroma_collection(cfg)
    seen = existing_hashes(collection)

    pending: list[Chunk] = []
    for c in chunks:
        if seen.get(c.chunk_id) == c.content_hash:
            continue
        pending.append(c)

    written = 0
    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        embeddings = encode_passages(cfg, [c.text for c in batch])
        collection.upsert(
            ids=[c.chunk_id for c in batch],
            embeddings=embeddings.tolist(),
            documents=[c.text for c in batch],
            metadatas=[
                {
                    "rel_source": c.rel_source,
                    "doc_title": c.doc_title,
                    "doc_type": c.doc_type,
                    "language": c.language,
                    "section_path": " > ".join(c.section_path) if c.section_path else "",
                    "chunk_index": c.chunk_index,
                    "content_hash": c.content_hash,
                    "page_start": c.page_start if c.page_start is not None else 0,
                }
                for c in batch
            ],
        )
        written += len(batch)
    return written


def delete_missing_sources(cfg: Config, present_rel_sources: set[str]) -> int:
    """Drop chunks whose rel_source is no longer in the corpus."""
    collection = get_chroma_collection(cfg)
    res = collection.get(include=["metadatas"])
    to_delete = [
        cid
        for cid, meta in zip(res.get("ids", []), res.get("metadatas", []))
        if meta and meta.get("rel_source") not in present_rel_sources
    ]
    if to_delete:
        collection.delete(ids=to_delete)
    return len(to_delete)


__all__ = [
    "COLLECTION_NAME",
    "encode_passages",
    "encode_query",
    "get_embedder",
    "token_count_fn",
    "get_chroma_collection",
    "upsert_chunks",
    "delete_missing_sources",
]
