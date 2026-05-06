"""Helper to stop Docker services for student-bot.

Usage:
  uv run student-bot-down
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click
import yaml

HOST_METRICS_STOP_CMD = "pkill -f student-bot-host-metrics"


def _performance_panel_enabled(config_path: Path) -> bool:
    if not config_path.exists():
        return False
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    web = data.get("web") if isinstance(data, dict) else None
    if not isinstance(web, dict):
        return False
    return bool(web.get("performance_panel_enabled", False))


@click.command()
def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config_path = root / "config.yaml"
    perf_on = _performance_panel_enabled(config_path)

    click.echo("Stopping beta-web and bot...")
    subprocess.run(["docker", "compose", "stop", "beta-web", "bot"], cwd=root, check=True)

    if perf_on:
        click.echo("If host metrics collector is still running, stop it with:")
        click.echo(HOST_METRICS_STOP_CMD)


if __name__ == "__main__":
    main()
