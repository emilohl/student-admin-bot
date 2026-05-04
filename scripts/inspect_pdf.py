"""Dump pymupdf4llm output (and optionally docling) for visual table-fidelity QA.

Usage:
    python -m scripts.inspect_pdf path/to/file.pdf
    python -m scripts.inspect_pdf path/to/file.pdf --docling
"""
from __future__ import annotations

from pathlib import Path

import click

from student_bot.config import PROJECT_ROOT


@click.command()
@click.argument("pdf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--docling", "use_docling", is_flag=True, help="Use docling instead of pymupdf4llm.")
def main(pdf_path: Path, use_docling: bool):
    out_dir = PROJECT_ROOT / "scripts" / "_inspect_out"
    out_dir.mkdir(exist_ok=True)
    suffix = "docling" if use_docling else "pymupdf4llm"
    out_path = out_dir / f"{pdf_path.stem}.{suffix}.md"

    if use_docling:
        from docling.document_converter import DocumentConverter
        text = DocumentConverter().convert(str(pdf_path)).document.export_to_markdown()
    else:
        import pymupdf4llm
        text = pymupdf4llm.to_markdown(str(pdf_path))

    out_path.write_text(text, encoding="utf-8")
    print(f"wrote {out_path}  ({len(text):,} chars)")


if __name__ == "__main__":
    main()
