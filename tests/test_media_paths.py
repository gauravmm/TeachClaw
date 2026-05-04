"""Tests for the per-user MediaRepository."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from benchclaw.bus import MessageAddress
from benchclaw.media import MediaRepository

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _addr(channel: str, chat_id: str) -> MessageAddress:
    return MessageAddress(channel, chat_id)


def _user_media_dir(workspace: Path, addr: MessageAddress) -> Path:
    return workspace / "storage" / addr.channel / addr.chat_id / "media"


def _meta_path(workspace: Path, addr: MessageAddress) -> Path:
    return workspace / "storage" / addr.channel / addr.chat_id / ".media.json"


def test_register_writes_under_per_user_storage(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    addr = _addr("telegram", "123456")
    ts = datetime(2026, 3, 10, 14, 23, 5)

    path = repo.register(
        addr,
        sender_id="123456",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )

    assert path == _user_media_dir(tmp_path, addr) / "20260310T142305-01.jpg"
    assert repo.model_relpath(path) == "media/20260310T142305-01.jpg"


def test_register_serial_increments_within_same_second(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    addr = _addr("telegram", "123456")
    ts = datetime(2026, 3, 10, 14, 23, 5)

    p1 = repo.register(
        addr, sender_id="x", media_type="image", ext=".jpg", mime_type="image/jpeg", timestamp=ts
    )
    p2 = repo.register(
        addr, sender_id="x", media_type="image", ext=".jpg", mime_type="image/jpeg", timestamp=ts
    )

    assert p1.name == "20260310T142305-01.jpg"
    assert p2.name == "20260310T142305-02.jpg"


def test_register_different_users_isolated(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    ts = datetime(2026, 3, 10, 14, 23, 0)

    p_alice = repo.register(
        _addr("telegram", "alice"),
        sender_id="alice",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )
    p_bob = repo.register(
        _addr("telegram", "bob"),
        sender_id="bob",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )

    assert p_alice.parent == _user_media_dir(tmp_path, _addr("telegram", "alice"))
    assert p_bob.parent == _user_media_dir(tmp_path, _addr("telegram", "bob"))
    assert p_alice.parent != p_bob.parent


def test_resolve_file_returns_absolute_path_and_mime(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    addr = _addr("telegram", "123456")
    path = repo.register(
        addr,
        sender_id="x",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=datetime(2026, 3, 10, 14, 23, 0),
    )
    path.write_bytes(b"jpeg-bytes")

    resolved, mime = repo.resolve_file(addr, repo.model_relpath(path))

    assert resolved == path
    assert mime == "image/jpeg"


def test_resolve_file_rejects_non_media_paths(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    addr = _addr("telegram", "123456")
    with pytest.raises(ValueError):
        repo.resolve_file(addr, "notes.md")
    with pytest.raises(ValueError):
        repo.resolve_file(addr, "/etc/passwd")
    with pytest.raises(ValueError):
        repo.resolve_file(addr, "media/sub/inner.jpg")


def test_set_caption_updates_user_metadata(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    addr = _addr("telegram", "555")
    path = repo.register(
        addr,
        sender_id="555",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=datetime(2026, 3, 10, 14, 23, 0),
    )
    path.write_bytes(b"jpeg-bytes")
    rel = repo.model_relpath(path)

    repo.set_caption(addr, rel, "a dog sitting on grass")

    meta = json.loads(_meta_path(tmp_path, addr).read_text(encoding="utf-8"))
    assert meta[rel]["caption"] == "a dog sitting on grass"


def test_image_block_returns_provider_payload(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    addr = _addr("telegram", "1")
    path = repo.register(
        addr,
        sender_id="1",
        media_type="image",
        ext=".png",
        mime_type="image/png",
        timestamp=datetime(2026, 3, 10, 14, 23, 0),
    )
    path.write_bytes(PNG_1X1)

    block = repo.image_block(addr, repo.model_relpath(path))

    assert block["type"] == "image_url"
    url = block["image_url"]["url"]  # type: ignore[index]
    assert isinstance(url, str)
    assert url.startswith("data:image/png;base64,")


def test_build_media_blocks_skips_missing_files(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    addr = _addr("telegram", "1")
    path = repo.register(
        addr,
        sender_id="1",
        media_type="image",
        ext=".png",
        mime_type="image/png",
        timestamp=datetime(2026, 3, 10, 14, 23, 0),
    )
    path.write_bytes(PNG_1X1)
    rel = repo.model_relpath(path)

    blocks = repo.build_media_blocks(addr, [rel, "media/missing.png"])

    assert len(blocks) == 1
    assert blocks[0]["type"] == "image_url"


def test_serial_rebuilt_after_reload(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    addr = _addr("telegram", "555")
    ts = datetime(2026, 3, 10, 14, 23, 0)

    p1 = repo.register(
        addr, sender_id="555", media_type="image", ext=".jpg", mime_type="image/jpeg", timestamp=ts
    )
    p1.touch()

    repo2 = MediaRepository(tmp_path)
    p2 = repo2.register(
        addr, sender_id="555", media_type="image", ext=".jpg", mime_type="image/jpeg", timestamp=ts
    )

    assert p1.name == "20260310T142300-01.jpg"
    assert p2.name == "20260310T142300-02.jpg"


def test_purge_old_only_removes_old_registered_media(tmp_path: Path):
    from datetime import timedelta as _td

    from benchclaw.utils import now_aware

    repo = MediaRepository(tmp_path, max_age_days=30)
    addr = _addr("telegram", "555")
    now = now_aware()

    old = repo.register(
        addr,
        sender_id="555",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=now - _td(days=90),
    )
    new = repo.register(
        addr,
        sender_id="555",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=now - _td(days=1),
    )
    old.touch()
    new.touch()

    deleted = repo._purge_old()

    assert deleted == 1
    assert not old.exists()
    assert new.exists()


def test_purge_old_skips_admin_dir(tmp_path: Path):
    (tmp_path / "storage" / "_admin").mkdir(parents=True)
    (tmp_path / "storage" / "_admin" / "secret.json").write_text("{}", encoding="utf-8")
    repo = MediaRepository(tmp_path, max_age_days=30)

    deleted = repo._purge_old()

    assert deleted == 0
    assert (tmp_path / "storage" / "_admin" / "secret.json").exists()
