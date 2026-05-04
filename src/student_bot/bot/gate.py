"""Off-topic / low-confidence gate.

Combines two signals — both must hold for the bot to attempt an answer:
  1. Reranker scores: top-1 ≥ T1 AND mean(top-meanK) ≥ T2.
  2. Source agreement: top-K chunks span ≤ M distinct documents.

The thresholds are empirical — tune from `eval/run_eval.py` output.
"""

from __future__ import annotations

from dataclasses import dataclass

from student_bot.config import Config
from student_bot.bot.retrieval import RetrievalResult


@dataclass
class GateDecision:
    passed: bool
    reason: str
    top1: float
    meanK: float
    distinct_sources: int


def evaluate(cfg: Config, result: RetrievalResult) -> GateDecision:
    if not result.reranked:
        return GateDecision(False, "no_candidates", 0.0, 0.0, 0)

    top_scores = [c.rerank_score for c in result.reranked]
    top1 = top_scores[0]
    k = min(cfg.gate.meanK, len(top_scores))
    meanK = sum(top_scores[:k]) / k
    distinct = len({c.rel_source for c in result.reranked})

    if top1 < cfg.gate.rerank_top1_min:
        return GateDecision(False, f"top1<{cfg.gate.rerank_top1_min}", top1, meanK, distinct)
    if meanK < cfg.gate.rerank_meanK_min:
        return GateDecision(False, f"meanK<{cfg.gate.rerank_meanK_min}", top1, meanK, distinct)
    if distinct > cfg.gate.max_distinct_sources_in_topk:
        return GateDecision(
            False, f"sources_spread>{cfg.gate.max_distinct_sources_in_topk}", top1, meanK, distinct
        )

    return GateDecision(True, "pass", top1, meanK, distinct)


__all__ = ["GateDecision", "evaluate"]
