"""Helper to start Docker services for student-bot.

Usage:
  uv run student-bot-up
  uv run student-bot-up --build
  uv run student-bot-up --dev
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click
import yaml


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
@click.option("--build", is_flag=True, help="Run `docker compose build` before start.")
@click.option(
    "--dev",
    is_flag=True,
    help="Use docker-compose.dev.yml (bind-mount src/scripts/eval for fast code iteration).",
)
def main(build: bool, dev: bool) -> None:
    root = Path(__file__).resolve().parents[1]
    config_path = root / "config.yaml"
    perf_on = _performance_panel_enabled(config_path)
    compose_cmd = ["docker", "compose"]
    if dev:
        compose_cmd.extend(["-f", "docker-compose.yml", "-f", "docker-compose.dev.yml"])

    if build:
        click.echo("Building Docker images...")
        subprocess.run([*compose_cmd, "build"], cwd=root, check=True)

    click.echo("Starting beta-web and bot...")
    subprocess.run([*compose_cmd, "up", "-d", "beta-web", "bot"], cwd=root, check=True)

    if perf_on:
        click.echo("Performance panel is enabled. Start host metrics collector:")
        click.echo("uv run student-bot-host-metrics")


if __name__ == "__main__":
    main()
