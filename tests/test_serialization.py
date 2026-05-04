"""Tests for MessageAddress and Session serialization round-trips."""

from datetime import datetime
from pathlib import Path

import pytest

from benchclaw.bus import MessageAddress
from benchclaw.session import (
    MAX_SESSIONS,
    AssistantEvent,
    Session,
    SessionManager,
    SummaryEvent,
    UserEvent,
)

# ---------------------------------------------------------------------------
# MessageAddress
# ---------------------------------------------------------------------------


def test_message_address_str():
    addr = MessageAddress(channel="telegram", chat_id="123")
    assert str(addr) == "telegram:123"


def test_message_address_from_string():
    addr = MessageAddress.from_string("telegram:123")
    assert addr.channel == "telegram"
    assert addr.chat_id == "123"


def test_message_address_from_string_roundtrip():
    addr = MessageAddress(channel="whatsapp", chat_id="456@s.whatsapp.net")
    assert MessageAddress.from_string(str(addr)) == addr


def test_message_address_from_string_colon_in_chat_id():
    """chat_id may itself contain colons; only the first colon is the delimiter."""
    addr = MessageAddress.from_string("telegram:123:456")
    assert addr.channel == "telegram"
    assert addr.chat_id == "123:456"


# ---------------------------------------------------------------------------
# Session.save / Session.load
# ---------------------------------------------------------------------------


def test_session_save_load_roundtrip(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="99")
    session = Session(addr=addr)
    session.append(
        UserEvent(
            content="hello",
            media=["workspace/media/telegram/99/20260308_101530/abc.jpg"],
            media_metadata=[
                {
                    "path": "workspace/media/telegram/99/20260308_101530/abc.jpg",
                    "media_type": "image",
                    "mime_type": "image/jpeg",
                    "size_bytes": 12345,
                    "saved_at": "2026-03-08T10:15:30",
                    "source_channel": "telegram",
                    "original_name": None,
                }
            ],
        )
    )
    session.append(AssistantEvent(content="hi there", metadata={"tools_used": ["search"]}))

    path = tmp_path / "session.jsonl"
    session.save(path)

    loaded = Session.load(path)
    assert loaded is not None
    assert loaded.addr == addr
    assert len(loaded.events) == 2
    assert loaded.events[0].content == "hello"
    assert loaded.events[0].media == ["workspace/media/telegram/99/20260308_101530/abc.jpg"]
    assert loaded.events[0].media_metadata[0]["media_type"] == "image"
    assert loaded.events[1].metadata["tools_used"] == ["search"]


def test_session_load_missing_file(tmp_path: Path):
    assert Session.load(tmp_path / "nonexistent.jsonl") is None


def test_session_load_missing_address(tmp_path: Path):
    """A JSONL file without an address field should return None."""
    path = tmp_path / "bad.jsonl"
    path.write_text('{"_type": "metadata", "created_at": "2024-01-01T00:00:00"}\n')
    assert Session.load(path) is None


def test_session_load_preserves_timestamps(tmp_path: Path):
    addr = MessageAddress(channel="smtp", chat_id="user@example.com")
    created = datetime(2024, 6, 1, 12, 0, 0).astimezone()
    session = Session(addr=addr, created_at=created)

    path = tmp_path / "session.jsonl"
    session.save(path)

    loaded = Session.load(path)
    assert loaded is not None
    assert loaded.created_at == created


def test_session_load_preserves_metadata(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="42")
    session = Session(addr=addr, metadata={"thread": "xyz"})

    path = tmp_path / "session.jsonl"
    session.save(path)

    loaded = Session.load(path)
    assert loaded is not None
    assert loaded.metadata == {"thread": "xyz"}


def test_session_clear(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="1")
    session = Session(addr=addr)
    session.append(UserEvent(content="test"))
    session.clear()
    assert session.events == []
    assert not session.has_summary


def test_session_compact_with_summary_replaces_history() -> None:
    session = Session(addr=MessageAddress(channel="telegram", chat_id="1"))
    session.append(UserEvent(content="hello"))
    session.append(UserEvent(content="world"))

    session.compact_with_summary("User said hello, then world.")

    assert len(session.events) == 1
    assert isinstance(session.events[0], SummaryEvent)
    assert session.events[0].content == "User said hello, then world."
    assert session.has_summary


def test_session_history_includes_sender_and_timestamp_prefix() -> None:
    addr = MessageAddress(channel="telegram", chat_id="1")
    session = Session(addr=addr)

    session.append(
        UserEvent(
            content="hello",
            sender_id="12345|gaurav",
            metadata={"sender_label": "Gaurav"},
        )
    )

    rendered = session.render_llm_messages("system")[-1]
    assert rendered["role"] == "user"
    assert rendered["content"].startswith("[Gaurav @")
    assert rendered["content"].endswith(": hello")


def test_session_history_includes_user_timestamp_prefix() -> None:
    addr = MessageAddress(channel="telegram", chat_id="2")
    session = Session(addr=addr)

    session.append(
        UserEvent(content="ping", sender_id="7|alice", metadata={"sender_label": "alice"})
    )

    rendered = session.render_llm_messages("system")
    assert rendered[-1]["role"] == "user"
    assert rendered[-1]["content"].startswith("[alice @")
    assert rendered[-1]["content"].endswith(": ping")


def test_session_describe_current_session_prefers_sender_label() -> None:
    session = Session(addr=MessageAddress(channel="telegram", chat_id="42"))
    session.append(
        UserEvent(
            content="hello",
            sender_id="7|alice",
            metadata={"sender_label": "Alice", "is_group": False},
        )
    )

    assert session.describe_current_session() == "Alice on Telegram"


def test_session_describe_current_session_handles_group_chats() -> None:
    session = Session(addr=MessageAddress(channel="whatsapp", chat_id="group-1"))
    session.append(
        UserEvent(
            content="hello",
            sender_id="7",
            metadata={"sender_label": "Alice", "is_group": True},
        )
    )

    assert session.describe_current_session() == "WhatsApp group chat (recent sender: Alice)"


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_manager_get_or_create(tmp_path: Path):
    async with SessionManager(tmp_path) as sm:
        addr = MessageAddress(channel="telegram", chat_id="1")
        s1 = sm.get(addr)
        s2 = sm.get(addr)
        assert s1 is s2


@pytest.mark.asyncio
async def test_session_manager_persists_on_exit(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="1")

    async with SessionManager(tmp_path) as sm:
        s = sm.get(addr)
        s.append(UserEvent(content="persisted"))

    # Re-enter and check the session was saved
    async with SessionManager(tmp_path) as sm2:
        s2 = sm2.get(addr)
        assert len(s2.events) == 1
        assert s2.events[0].content == "persisted"


@pytest.mark.asyncio
async def test_session_manager_save_midway(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="2")

    async with SessionManager(tmp_path) as sm:
        s = sm.get(addr)
        s.append(UserEvent(content="mid"))
        sm.save(s)

    path = tmp_path / "telegram2.jsonl"
    assert path.exists()
    loaded = Session.load(path)
    assert loaded is not None
    assert loaded.events[0].content == "mid"


@pytest.mark.asyncio
async def test_session_manager_clear_archives(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="3")

    async with SessionManager(tmp_path) as sm:
        s = sm.get(addr)
        s.append(UserEvent(content="to be archived"))
        sm.save(s)
        sm.clear(addr)

    archive_dir = tmp_path / ".archive"
    archived = list(archive_dir.glob("*.jsonl"))
    assert len(archived) == 1


@pytest.mark.asyncio
async def test_session_manager_max_sessions(tmp_path: Path):
    """Sessions beyond MAX_SESSIONS are archived on __aenter__."""
    # Pre-create MAX_SESSIONS + 5 session files
    for i in range(MAX_SESSIONS + 5):
        addr = MessageAddress(channel="telegram", chat_id=str(i))
        s = Session(addr=addr, updated_at=datetime(2024, 1, 1, hour=i % 24))
        path = tmp_path / f"telegram{i}.jsonl"
        s.save(path)

    async with SessionManager(tmp_path) as sm:
        assert len(sm._cache) == MAX_SESSIONS

    archive_dir = tmp_path / ".archive"
    archived = list(archive_dir.glob("*.jsonl"))
    assert len(archived) == 5
