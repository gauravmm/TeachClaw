from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from teachclaw import auth as auth_module
from teachclaw import storage as storage_layout
from teachclaw.bus import MessageAddress


def _addr() -> MessageAddress:
    return MessageAddress("telegram", "12345")


def test_secret_round_trip(tmp_path: Path) -> None:
    storage_layout.ensure_user_dirs(tmp_path, _addr())
    record = auth_module.write_secret(tmp_path, "ABCDEF")
    assert record.code == "ABCDEF"
    loaded = auth_module.read_secret(tmp_path)
    assert loaded is not None
    assert loaded.code == "ABCDEF"


def test_marker_drives_authentication_state(tmp_path: Path) -> None:
    addr = _addr()
    storage_layout.ensure_user_dirs(tmp_path, addr)
    auth_module.write_secret(tmp_path, "K7P3WQ")

    assert not auth_module.is_authenticated(tmp_path, addr)

    auth_module.write_marker(tmp_path, addr, "K7P3WQ")
    assert auth_module.is_authenticated(tmp_path, addr)

    # Rotating the secret bounces this marker.
    auth_module.write_secret(tmp_path, "DIFFR8")
    assert not auth_module.is_authenticated(tmp_path, addr)


def test_authenticated_addresses_filters_by_current_secret(tmp_path: Path) -> None:
    a = MessageAddress("telegram", "u1")
    b = MessageAddress("telegram", "u2")
    auth_module.write_secret(tmp_path, "AAAAAA")
    storage_layout.ensure_user_dirs(tmp_path, a)
    storage_layout.ensure_user_dirs(tmp_path, b)
    auth_module.write_marker(tmp_path, a, "AAAAAA")
    auth_module.write_marker(tmp_path, b, "BBBBBB")  # stale code

    listed = auth_module.authenticated_addresses(tmp_path, "telegram")
    assert listed == ["u1"]


def test_rate_limiter_locks_after_threshold() -> None:
    limiter = auth_module.AuthRateLimiter()
    user = "u1"
    for i in range(auth_module.RATE_LIMIT_FAILURES - 1):
        failures, locked = limiter.record_failure(user)
        assert not locked
        assert failures == i + 1
    failures, locked = limiter.record_failure(user)
    assert locked

    ok, msg = limiter.check(user)
    assert not ok
    assert msg and "minute" in msg


def test_rate_limiter_success_clears_state() -> None:
    limiter = auth_module.AuthRateLimiter()
    limiter.record_failure("u1")
    limiter.record_failure("u1")
    limiter.record_success("u1")
    ok, _ = limiter.check("u1")
    assert ok


def test_generate_code_uses_alphabet() -> None:
    for _ in range(20):
        code = auth_module.generate_code()
        assert len(code) == auth_module.SECRET_LENGTH
        assert all(c in auth_module.SECRET_ALPHABET for c in code)


def test_constants_are_sensible() -> None:
    assert auth_module.RATE_LIMIT_WINDOW == timedelta(minutes=10)
    assert auth_module.RATE_LIMIT_LOCKOUT == timedelta(hours=1)
