"""Live smoke test for the study-plan structured chunker.

For each program in ``--codes``, runs the full ``maybe_fetch_dynamic_web``
flow against KTH and reports per-topic chunk presence so you can spot atlas
drift after a KTH redesign. Hits the network — not run in CI.

Run:
    uv run python -m scripts.smoke_studyplan_coverage
    uv run python -m scripts.smoke_studyplan_coverage --codes CTFYS,CDATE,CINEK
    uv run python -m scripts.smoke_studyplan_coverage --term HT2024
"""

from __future__ import annotations

from collections import Counter

import click

from student_bot.bot.web_retrieval import maybe_fetch_dynamic_web
from student_bot.config import get_config


@click.command()
@click.option(
    "--codes",
    default="CTFYS,CDATE,CINEK,CMEDT,CMAST",
    help="Comma-separated 5-letter program codes.",
)
@click.option("--term", default="HT2024", help="Admission round to ask about.")
def main(codes: str, term: str) -> None:
    cfg = get_config()
    code_list = [c.strip().upper() for c in codes.split(",") if c.strip()]
    for code in code_list:
        question = f"Vilka masterprogram är valbara för {code} {term}?"
        print(f"\n=== {code} ({term}) ===")
        print(f"q: {question}")
        res = maybe_fetch_dynamic_web(cfg, question, "sv")
        if res is None:
            print("  result: None")
            continue
        if res.clarification:
            print(f"  CLARIFICATION: {res.clarification[0][:160]}")
            continue
        chunks = res.chunks
        print(f"  chunks={len(chunks)} resolved={res.resolved_program_code}")
        topics = Counter(c.section_path.split(" (")[0] for c in chunks if c.section_path)
        for topic, n in topics.most_common(10):
            print(f"    [{n:2d}] {topic}")
        master_chunks = [c for c in chunks if "Valbara masterprogram" in c.section_path]
        if master_chunks:
            print(f"  Valbara masterprogram chunks ({len(master_chunks)}):")
            for c in master_chunks[:3]:
                print(f"    fält: {c.rel_source.split('#')[-1]:36} | {c.text[:90]!r}")
        else:
            print("  ⚠ no 'Valbara masterprogram' chunks — atlas may need updating")


if __name__ == "__main__":
    main()
