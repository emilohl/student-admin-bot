"""Walk the corpus, parse, chunk, embed, upsert. Idempotent via content hashes."""

from __future__ import annotations

import sys
from collections import Counter

import click
from rich.console import Console
from rich.table import Table

from student_bot.config import get_config
from student_bot.ingest.chunk import chunk_document
from student_bot.ingest.embed import (
    delete_missing_sources,
    get_chroma_collection,
    token_count_fn,
    upsert_chunks,
)
from student_bot.ingest.parse import iter_corpus, parse_file


@click.command()
@click.option("--dry-run", is_flag=True, help="Parse and chunk but don't embed/write.")
@click.option("--limit", type=int, default=None, help="Process at most N files.")
def main(dry_run: bool, limit: int | None):
    cfg = get_config()
    console = Console()
    docs_root = cfg.absolute(cfg.paths.docs_dir).resolve()
    if not docs_root.exists():
        console.print(f"[red]docs_dir does not exist: {docs_root}[/red]")
        sys.exit(1)

    files = iter_corpus(cfg)
    if limit:
        files = files[:limit]
    console.print(f"Found [bold]{len(files)}[/bold] candidate files under {docs_root}")

    if dry_run:
        # cheap approx; avoids loading the embedding model
        def count_tokens(s: str) -> int:
            return max(1, len(s) // 4)
    else:
        count_tokens = token_count_fn(cfg)

    docs = []
    skipped = []
    for path in files:
        try:
            d = parse_file(path, cfg, docs_root)
        except Exception as e:
            console.print(f"[red]parse fail[/red] {path.name}: {e}")
            skipped.append((path, str(e)))
            continue
        if d is None:
            console.print(f"[yellow]parsed empty[/yellow] {path.relative_to(docs_root)}")
            continue
        docs.append(d)

    all_chunks = []
    per_file_counts: list[tuple[str, int, str, str]] = []
    for d in docs:
        chunks = chunk_document(d, cfg, count_tokens)
        all_chunks.extend(chunks)
        per_file_counts.append((d.rel_source, len(chunks), d.language, d.doc_type))

    type_counts = Counter(d.doc_type for d in docs)
    lang_counts = Counter(d.language for d in docs)

    table = Table(title="Ingest summary")
    table.add_column("metric")
    table.add_column("value")
    table.add_row("files parsed", str(len(docs)))
    table.add_row("files skipped", str(len(skipped)))
    table.add_row("chunks total", str(len(all_chunks)))
    table.add_row("by type", str(dict(type_counts)))
    table.add_row("by language", str(dict(lang_counts)))
    console.print(table)

    if dry_run:
        sample = Table(title="Per-file chunk counts (first 20)")
        sample.add_column("rel_source")
        sample.add_column("chunks", justify="right")
        sample.add_column("lang")
        sample.add_column("type")
        for row in per_file_counts[:20]:
            sample.add_row(row[0], str(row[1]), row[2], row[3])
        console.print(sample)
        return

    written = upsert_chunks(cfg, all_chunks)
    present = {d.rel_source for d in docs}
    present_ids = {c.chunk_id for c in all_chunks}
    deleted = delete_missing_sources(cfg, present, present_ids)
    coll = get_chroma_collection(cfg)
    console.print(
        f"[green]upserted={written}[/green] [yellow]deleted={deleted}[/yellow] "
        f"collection_size={coll.count()}"
    )


if __name__ == "__main__":
    main()
