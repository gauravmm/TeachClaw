"""Shared-secret session gate. See spec/AUTH.md.

The current secret lives at ``storage/_admin/secret.json`` and is read by the
bot service process — never via tools, since ``storage/_admin/`` sits outside
every user's sandbox. Per-user auth markers live at
``storage/<channel>/<user_id>/auth.json`` and store the actual code the user
authenticated against (not a version number — see AUTH.md for why).

Rate-limit counters are kept in-memory by the channel; rotating the secret
bounces every previously-authenticated user on their next request.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from teachclaw import storage as storage_layout
from teachclaw.bus import MessageAddress
from teachclaw.utils import now_aware

SECRET_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SECRET_LENGTH = 6
RATE_LIMIT_FAILURES = 5
RATE_LIMIT_WINDOW = timedelta(minutes=10)
RATE_LIMIT_LOCKOUT = timedelta(hours=1)


@dataclass(frozen=True)
class SecretRecord:
    code: str
    set_at: datetime


def secret_path(workspace: Path) -> Path:
    return storage_layout.admin_dir(workspace) / "secret.json"


def auth_marker_path(workspace: Path, addr: MessageAddress) -> Path:
    return storage_layout.storage_root(workspace, addr) / "auth.json"


def generate_code() -> str:
    return "".join(secrets.choice(SECRET_ALPHABET) for _ in range(SECRET_LENGTH))


def normalize_code(code: str) -> str:
    """Uppercase + strip whitespace; alphabet has no confusables so no further mapping."""
    return code.strip().upper()


def is_valid_code_shape(code: str) -> bool:
    return len(code) >= 3 and all(c in SECRET_ALPHABET for c in code)


def read_secret(workspace: Path) -> SecretRecord | None:
    path = secret_path(workspace)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SecretRecord(
            code=str(data["code"]),
            set_at=datetime.fromisoformat(data["set_at"]),
        )
    except OSError, ValueError, KeyError:
        return None


def write_secret(workspace: Path, code: str) -> SecretRecord:
    storage_layout.admin_dir(workspace).mkdir(parents=True, exist_ok=True, mode=0o700)
    record = SecretRecord(code=code, set_at=now_aware())
    path = secret_path(workspace)
    path.write_text(
        json.dumps({"code": record.code, "set_at": record.set_at.isoformat(timespec="seconds")}),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return record


def read_marker(workspace: Path, addr: MessageAddress) -> str | None:
    path = auth_marker_path(workspace, addr)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("code") or "")
    except OSError, ValueError:
        return None


def write_marker(workspace: Path, addr: MessageAddress, code: str) -> None:
    path = auth_marker_path(workspace, addr)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "code": code,
        "authenticated_at": now_aware().isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def is_authenticated(workspace: Path, addr: MessageAddress) -> bool:
    secret = read_secret(workspace)
    if secret is None:
        return False
    return read_marker(workspace, addr) == secret.code


def authenticated_addresses(workspace: Path, channel: str) -> list[str]:
    """Scan storage/<channel>/* for users whose marker matches the current secret."""
    secret = read_secret(workspace)
    if secret is None:
        return []
    base = workspace / "storage" / channel
    if not base.exists():
        return []
    out: list[str] = []
    for child in sorted(base.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        addr = MessageAddress(channel=channel, chat_id=child.name)
        if read_marker(workspace, addr) == secret.code:
            out.append(child.name)
    return out


@dataclass
class _RateState:
    failures: int = 0
    window_started: datetime | None = None
    locked_until: datetime | None = None


class AuthRateLimiter:
    """In-memory per-user failure counter for /auth attempts."""

    def __init__(self) -> None:
        self._state: dict[str, _RateState] = {}

    def _state_for(self, user_id: str) -> _RateState:
        return self._state.setdefault(user_id, _RateState())

    def check(self, user_id: str) -> tuple[bool, str | None]:
        """Return (allowed, message). If not allowed, the caller should reply with the message."""
        st = self._state_for(user_id)
        now = now_aware()
        if st.locked_until and now < st.locked_until:
            mins = int((st.locked_until - now).total_seconds() // 60) + 1
            return False, f"Too many wrong codes. Try again in ~{mins} minute(s)."
        if st.locked_until and now >= st.locked_until:
            st.locked_until = None
            st.failures = 0
            st.window_started = None
        return True, None

    def record_failure(self, user_id: str) -> tuple[int, bool]:
        """Record a failed attempt; return (failures_in_window, locked_now)."""
        st = self._state_for(user_id)
        now = now_aware()
        if st.window_started is None or now - st.window_started > RATE_LIMIT_WINDOW:
            st.window_started = now
            st.failures = 0
        st.failures += 1
        if st.failures >= RATE_LIMIT_FAILURES:
            st.locked_until = now + RATE_LIMIT_LOCKOUT
            return st.failures, True
        return st.failures, False

    def record_success(self, user_id: str) -> None:
        self._state.pop(user_id, None)
