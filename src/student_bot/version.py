"""Runtime version detection for display in user-facing surfaces.

Resolution order (first match wins):

1. ``STUDENT_BOT_VERSION`` env var — explicit override, intended for Docker
   builds where the source tree is detached from ``.git/``. Set it at build
   time, e.g. ``ARG STUDENT_BOT_VERSION=$(git rev-parse --short HEAD)``.
2. Git tag at HEAD (via ``git describe --tags --exact-match``) — release
   version such as ``v1.2.3``.
3. Short git commit hash (``git rev-parse --short HEAD``) — dev builds.
4. Empty result when none of the above succeed.

The result is computed once at import time and cached.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("student_bot")

REPO_URL = "https://github.com/cohm/student-admin-bot"


@dataclass(frozen=True)
class VersionInfo:
    display: str  # what to show; "" means version is unknown
    link: str  # GitHub URL for the release/commit; "" when display is empty
    is_release: bool


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("git %s failed: %s", " ".join(args), e)
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace").strip()


def _looks_like_release_tag(s: str) -> bool:
    s = s.lstrip("v")
    return bool(s) and s[0].isdigit()


@lru_cache(maxsize=1)
def get_version() -> VersionInfo:
    explicit = (os.environ.get("STUDENT_BOT_VERSION") or "").strip()
    if explicit:
        is_release = _looks_like_release_tag(explicit)
        link = (
            f"{REPO_URL}/releases/tag/{explicit}" if is_release else f"{REPO_URL}/commit/{explicit}"
        )
        return VersionInfo(display=explicit, link=link, is_release=is_release)

    tag = _git("describe", "--tags", "--exact-match", "HEAD")
    if tag:
        return VersionInfo(
            display=tag,
            link=f"{REPO_URL}/releases/tag/{tag}",
            is_release=True,
        )

    sha = _git("rev-parse", "--short", "HEAD")
    if sha:
        return VersionInfo(
            display=sha,
            link=f"{REPO_URL}/commit/{sha}",
            is_release=False,
        )

    return VersionInfo(display="", link="", is_release=False)


__all__ = ["VersionInfo", "REPO_URL", "get_version"]
