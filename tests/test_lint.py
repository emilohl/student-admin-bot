"""Run the same ruff checks CI runs, so `pytest` catches lint/format
regressions locally before they reach a PR.

Mirrors `.github/workflows/ci.yml` exactly: `ruff check .` and
`ruff format --check .` across the repo root. Skipped if the `ruff`
executable isn't on PATH (e.g. someone ran `uv sync` without `--extra dev`).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_ruff_check():
    result = subprocess.run(
        ["ruff", "check", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"ruff check failed:\n{result.stdout}{result.stderr}"


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_ruff_format():
    result = subprocess.run(
        ["ruff", "format", "--check", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"ruff format check failed (run `uv run ruff format .` to fix):\n"
        f"{result.stdout}{result.stderr}"
    )
