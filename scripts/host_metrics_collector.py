"""Collect host-machine load metrics and write them to data/host_metrics.json.

Intended for macOS host when app runs in Docker. The web app inside container
reads this file (bind-mounted via ./data) and shows host load beside container
load in the telemetry panel.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path


def _cpu_pct() -> float:
    load1 = os.getloadavg()[0]
    cpu_count = max(1, os.cpu_count() or 1)
    return max(0.0, min(100.0, (load1 / cpu_count) * 100.0))


def _mem_pct_macos() -> float | None:
    try:
        vm = subprocess.check_output(["vm_stat"], text=True, timeout=1.0)
        vals: dict[str, int] = {}
        for line in vm.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            num = int(re.sub(r"[^0-9]", "", v) or "0")
            vals[k.strip()] = num
        free = vals.get("Pages free", 0)
        speculative = vals.get("Pages speculative", 0)
        active = vals.get("Pages active", 0)
        inactive = vals.get("Pages inactive", 0)
        wired = vals.get("Pages wired down", 0)
        compressed = vals.get("Pages occupied by compressor", 0)
        total_pages = free + speculative + active + inactive + wired + compressed
        used_pages = active + inactive + wired + compressed
        if total_pages <= 0:
            return None
        return max(0.0, min(100.0, (used_pages / total_pages) * 100.0))
    except Exception:
        return None


def _gpu_pct_best_effort() -> float | None:
    # macOS has no stable non-privileged universal GPU util API.
    return None


def sample() -> dict:
    return {
        "ts_ms": int(time.time() * 1000),
        "cpu_pct": _cpu_pct(),
        "mem_pct": _mem_pct_macos(),
        "gpu": {"util_pct": _gpu_pct_best_effort(), "mem_pct": None},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("data/host_metrics.json"),
        help="output json path",
    )
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    while True:
        payload = sample()
        tmp = args.out.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(args.out)
        time.sleep(max(0.2, args.interval))


if __name__ == "__main__":
    main()
