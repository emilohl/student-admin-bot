"""Capability-URL token gate + HTTP Basic Auth.

Two-factor by design:
1. The user must visit a URL containing the secret access token
   (`?access=<WEB_ACCESS_TOKEN>`). On success, a signed session cookie
   (the `granted` flag) is set so the user doesn't need the token again
   until idle timeout.
2. Every chat / docs / stats endpoint then requires HTTP Basic Auth
   against credentials from `data/web_users` (or another `users_file`).

When `web.auth_enabled` is false, both gates are bypassed — appropriate for
a localhost-only deployment.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from fastapi import HTTPException, Request, status

from student_bot.config import Config


SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


# --- password hashing (stdlib only) ---


def hash_password(password: str) -> str:
    """Returns a passwd-style record body: 'scrypt:<salt_b64>:<hash_b64>'."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f"scrypt:{base64.b64encode(salt).decode()}:{base64.b64encode(digest).decode()}"


def verify_password(password: str, record: str) -> bool:
    try:
        scheme, salt_b64, hash_b64 = record.split(":")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except Exception:
        return False
    actual = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return hmac.compare_digest(actual, expected)


def list_usernames(cfg: Config) -> list[str]:
    """Return registered web usernames (sorted), or [] if no file configured."""
    if not cfg.web.users_file:
        return []
    users_path = cfg.absolute(Path(cfg.web.users_file))
    return sorted(load_users_file(users_path).keys())


class UserRecord(NamedTuple):
    """One parsed line of `data/web_users`.

    `record` is always the 3-field scrypt blob `scheme:salt_b64:hash_b64`
    (what `verify_password` expects). `is_admin` is True when the optional
    4th field on the line equals `admin`.
    """

    record: str
    is_admin: bool = False


def load_users_file(path: Path) -> dict[str, UserRecord]:
    """Parse '<user>:<password_record>[:admin]' per line.

    Lines starting with # are ignored. Lines with fewer than 3 record
    fields (after the username) are skipped silently so a malformed line
    doesn't lock everyone out.
    """
    users: dict[str, UserRecord] = {}
    if not path.exists():
        return users
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        username, _, rest = line.partition(":")
        if not username or not rest:
            continue
        # rest = scheme:salt_b64:hash_b64[:admin]
        parts = rest.split(":")
        if len(parts) < 3:
            continue
        record = ":".join(parts[:3])
        is_admin = len(parts) >= 4 and parts[3].strip().lower() == "admin"
        users[username] = UserRecord(record=record, is_admin=is_admin)
    return users


# --- request gates ---


@dataclass
class AuthContext:
    enabled: bool
    user: str | None  # HTTP Basic username, if authenticated
    granted: bool  # token-cookie granted
    name: str | None  # user-supplied display name
    is_admin: bool = False  # set from the optional `:admin` 4th field


def _expected_token() -> str | None:
    return os.environ.get("WEB_ACCESS_TOKEN")


def check_token_grant(request: Request, cfg: Config) -> bool:
    """Returns True if the request has a valid session-grant cookie OR
    presents a valid `?access=<token>` query param (in which case the cookie
    is set on the response by the middleware)."""
    if not cfg.web.auth_enabled:
        return True

    expected = _expected_token()
    if not expected:
        # Auth enabled but no token configured — fail closed.
        return False

    sess = request.session
    granted_at = sess.get("granted_at", 0)
    idle = cfg.web.session_idle_minutes * 60
    if granted_at and (time.time() - granted_at) <= idle:
        # Refresh sliding window on each successful gate check.
        sess["granted_at"] = time.time()
        return True

    token = request.query_params.get("access", "")
    if token and hmac.compare_digest(token, expected):
        sess["granted_at"] = time.time()
        return True

    return False


def basic_auth(request: Request, cfg: Config) -> tuple[str, bool] | None:
    """Validates HTTP Basic credentials.

    Returns (username, is_admin) on success, or None on missing/bad creds.
    When `cfg.web.auth_enabled` is False, returns ("anonymous", False).
    """
    if not cfg.web.auth_enabled:
        return ("anonymous", False)

    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(auth[6:]).decode("utf-8")
        username, _, password = raw.partition(":")
    except Exception:
        return None
    if not username:
        return None

    users_path = cfg.absolute(Path(cfg.web.users_file))
    users = load_users_file(users_path)
    info = users.get(username)
    if info is None:
        return None
    if not verify_password(password, info.record):
        return None
    return (username, info.is_admin)


def require_access(request: Request, cfg: Config) -> AuthContext:
    """Enforce both gates. Raises HTTPException on failure."""
    if not cfg.web.auth_enabled:
        return AuthContext(False, "anonymous", True, request.session.get("name"), False)

    if not check_token_grant(request, cfg):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid access token. Use the link you were given.",
        )
    auth_info = basic_auth(request, cfg)
    if not auth_info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="student-bot"'},
        )
    user, is_admin = auth_info
    return AuthContext(True, user, True, request.session.get("name"), is_admin)


__all__ = [
    "AuthContext",
    "UserRecord",
    "hash_password",
    "verify_password",
    "load_users_file",
    "list_usernames",
    "check_token_grant",
    "basic_auth",
    "require_access",
]
