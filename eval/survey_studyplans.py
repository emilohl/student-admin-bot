"""Survey which studyProgramme.* fields are populated across active programs.

Atlas-maintenance helper. Walks every 5-letter program code from
``data/program_aliases.json``, fetches ``/omfattning?l=sv`` for the most
recent admission term, and reports which ``studyProgramme.*`` fields carry
≥20 stripped chars.

Use this when KTH redesigns or after adding a new topic to the atlas, to
spot fields that the atlas (``data/study_plan_atlas.yaml``) doesn't yet
cover.

Run:
    uv run python -m eval.survey_studyplans
    uv run python -m eval.survey_studyplans --limit 5
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen

import click

from student_bot.bot.study_plan_atlas import get_atlas
from student_bot.bot.web_retrieval import (
    _compressed_application_store,
    _normalized_programme_terms_from_store,
    _strip_html_to_text,
)
from student_bot.config import PROJECT_ROOT, get_config


def _fetch(url: str, timeout: float = 8.0) -> str | None:
    req = Request(url, headers={"User-Agent": "student-bot/0.1 (atlas-survey)"})
    try:
        return urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _populated_fields(sp: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    if not isinstance(sp, dict):
        return out
    for k, v in sp.items():
        if isinstance(v, str):
            length = len(_strip_html_to_text(v))
        elif isinstance(v, list):
            length = len(v)
        elif isinstance(v, dict):
            length = sum(1 for _ in v.values() if _)
        else:
            length = 0
        if length >= 20 if isinstance(v, str) else length > 0:
            out[k] = length
    return out


@click.command()
@click.option("--limit", type=int, default=None, help="Survey at most N programs.")
@click.option(
    "--out",
    "out_path",
    type=click.Path(),
    default=None,
    help="Where to write the per-program JSON inventory (default: data/study_plan_field_inventory.json).",
)
def main(limit: int | None, out_path: str | None) -> None:
    cfg = get_config()
    aliases_path = cfg.absolute(Path(cfg.dynamic_web.program_aliases_file))
    with aliases_path.open("r", encoding="utf-8") as f:
        aliases = json.load(f).get("aliases", {})
    codes = sorted({v for v in aliases.values() if isinstance(v, str) and len(v) == 5})
    if limit:
        codes = codes[:limit]
    print(f"surveying {len(codes)} programs…")

    atlas = get_atlas(cfg)
    coverage = Counter()
    inventory: dict[str, dict] = {}
    uncovered: Counter = Counter()

    for code in codes:
        # Discover most recent term from the program root (cheap).
        root_html = _fetch(f"https://www.kth.se/student/kurser/program/{code}")
        if not root_html:
            print(f"  {code}: root fetch failed")
            continue
        terms = _normalized_programme_terms_from_store(_compressed_application_store(root_html))
        if not terms:
            print(f"  {code}: no terms; probably discontinued")
            inventory[code] = {"term": None, "fields": {}}
            continue
        term = terms[0]
        html = _fetch(f"https://www.kth.se/student/kurser/program/{code}/{term}/omfattning")
        if not html:
            print(f"  {code}: omfattning fetch failed")
            continue
        store = _compressed_application_store(html)
        sp = (store or {}).get("studyProgramme") if isinstance(store, dict) else None
        fields = _populated_fields(sp) if isinstance(sp, dict) else {}
        inventory[code] = {"term": term, "fields": fields}
        for k in fields:
            coverage[k] += 1
            if k not in atlas.field_to_topic:
                uncovered[k] += 1
        time.sleep(0.2)  # be polite to KTH

    print(f"\nfield coverage across {len(inventory)} programs:")
    for fld, n in coverage.most_common():
        topic = atlas.field_to_topic.get(fld, "—")
        marker = "  " if fld in atlas.field_to_topic else "??"
        print(f"  {marker} {fld:38} {n:3d}  -> {topic}")

    if uncovered:
        print("\nfields not in atlas (consider adding):")
        for fld, n in uncovered.most_common():
            print(f"  {fld:38} {n:3d}")

    out = Path(out_path) if out_path else PROJECT_ROOT / "data" / "study_plan_field_inventory.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump(
            {"fetched_at": int(time.time()), "programs": inventory},
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
