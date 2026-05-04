"""Evaluate retrieval recall and gate behaviour against eval/eval_set.yml.

Outputs:
  - recall@5 over in-domain queries
  - reranker score distributions for in-domain vs out-of-domain
  - suggested T1, T2 thresholds at the in-domain / OOD crossover (biased
    toward false-refuse: we pick the higher of the two crossovers)
  - gate accuracy under current config thresholds (in-domain pass-rate,
    OOD refuse-rate)

Does not call the LLM; retrieval + gate only.
"""

from __future__ import annotations

import statistics
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from student_bot.bot.gate import evaluate as evaluate_gate
from student_bot.bot.retrieval import retrieve
from student_bot.config import PROJECT_ROOT, get_config
from student_bot.jargon import Jargon


EVAL_FILE = PROJECT_ROOT / "eval" / "eval_set.yml"


def _matches_expected(rel_sources: list[str], entry: dict) -> bool:
    expected = entry.get("expected_any") or [entry.get("expected_doc_substring", "")]
    expected = [e.lower() for e in expected if e]
    if not expected:
        return False
    for src in rel_sources:
        s = src.lower()
        if any(e in s for e in expected):
            return True
    return False


def _suggest_threshold(in_scores: list[float], ood_scores: list[float]) -> float:
    """Pick the lowest in-domain score above the highest OOD score, if separable.
    Otherwise return the OOD median + 0.05 as a permissive starting point."""
    if not in_scores:
        return 0.0
    if not ood_scores:
        return min(in_scores)
    max_ood = max(ood_scores)
    sorted_in = sorted(in_scores)
    above = [s for s in sorted_in if s > max_ood]
    if above:
        return above[0]
    return statistics.median(ood_scores) + 0.05


@click.command()
@click.option("--eval-file", type=click.Path(path_type=Path), default=EVAL_FILE)
@click.option(
    "--show-failures", is_flag=True, help="Print details for queries that miss expected doc."
)
def main(eval_file: Path, show_failures: bool):
    cfg = get_config()
    console = Console()
    entries: list[dict] = yaml.safe_load(eval_file.read_text(encoding="utf-8"))
    # Run queries through the same jargon expansion the bot uses, so the
    # eval reflects production behaviour rather than bare retrieval.
    jargon = Jargon.from_config(cfg) if cfg.jargon.enabled else None

    in_top1: list[float] = []
    in_meanK: list[float] = []
    ood_top1: list[float] = []
    ood_meanK: list[float] = []
    in_recall_hits = 0
    in_total = 0
    in_pass = 0
    ood_refuse = 0
    failures: list[tuple[str, list[str]]] = []

    for entry in entries:
        q = entry["question"]
        kind = entry["kind"]
        if jargon is not None:
            q_used, _ = jargon.expand_query(q, lang=entry.get("lang"))
        else:
            q_used = q
        result = retrieve(cfg, q_used)
        gate = evaluate_gate(cfg, result)

        if kind == "in_domain":
            in_total += 1
            in_top1.append(gate.top1)
            in_meanK.append(gate.meanK)
            rel_sources = [c.rel_source for c in result.reranked]
            if _matches_expected(rel_sources, entry):
                in_recall_hits += 1
            else:
                failures.append((q, rel_sources))
            if gate.passed:
                in_pass += 1
        else:
            ood_top1.append(gate.top1)
            ood_meanK.append(gate.meanK)
            if not gate.passed:
                ood_refuse += 1

    # --- summary table ---
    summary = Table(title="Evaluation summary")
    summary.add_column("metric")
    summary.add_column("value", justify="right")
    summary.add_row("in-domain queries", str(in_total))
    summary.add_row("OOD queries", str(len(ood_top1)))
    summary.add_row(
        "recall@5 (in-domain)",
        f"{in_recall_hits}/{in_total} = {in_recall_hits / max(1, in_total):.0%}",
    )
    summary.add_row(
        "in-domain gate pass-rate",
        f"{in_pass}/{in_total} = {in_pass / max(1, in_total):.0%}",
    )
    summary.add_row(
        "OOD gate refuse-rate",
        f"{ood_refuse}/{len(ood_top1)} = {ood_refuse / max(1, len(ood_top1)):.0%}",
    )
    if in_top1:
        summary.add_row(
            "in-domain top1: min/median/max",
            f"{min(in_top1):.3f} / {statistics.median(in_top1):.3f} / {max(in_top1):.3f}",
        )
    if ood_top1:
        summary.add_row(
            "OOD top1: min/median/max",
            f"{min(ood_top1):.3f} / {statistics.median(ood_top1):.3f} / {max(ood_top1):.3f}",
        )
    console.print(summary)

    # --- suggested thresholds ---
    sug_top1 = _suggest_threshold(in_top1, ood_top1)
    sug_meanK = _suggest_threshold(in_meanK, ood_meanK)
    console.print(
        f"[bold]Suggested gate thresholds:[/bold] "
        f"rerank_top1_min={sug_top1:.3f}  rerank_meanK_min={sug_meanK:.3f}"
    )
    console.print(
        f"[dim]Current config: rerank_top1_min={cfg.gate.rerank_top1_min}  "
        f"rerank_meanK_min={cfg.gate.rerank_meanK_min}[/dim]"
    )

    if show_failures and failures:
        console.print("\n[bold red]Recall failures (expected doc not in top-K):[/bold red]")
        for q, srcs in failures:
            console.print(f"  Q: {q}")
            for s in srcs:
                console.print(f"    - {s}")


if __name__ == "__main__":
    main()
