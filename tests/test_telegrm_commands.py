"""Tests for the targeted state-mutating Telegram commands.

The handlers are tightly coupled to python-telegram-bot's ``Update`` /
``Context`` types and to ``TelegramChannel``; rather than spinning up a
real Application, we feed in lightweight fakes that satisfy the small
attribute surface each handler actually touches. We only cover the
paths that mutate shared state — auth markers, the bus, personality
files. The keyboard / menu / callback dispatch surfaces remain
intentionally untested (high mocking cost, low correctness risk).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from teachclaw import auth as auth_module
from teachclaw import personalities
from teachclaw import storage as storage_layout
from teachclaw.bus import MessageAddress, MessageBus, SessionControlEvent
from teachclaw.channels.telegrm import commands as cmd_module

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeUserState:
    replies: dict = field(default_factory=dict)
    seen_first_citation: bool = False
    served_reactions: set = field(default_factory=set)
    in_flight: bool = False


@dataclass
class _FakeChat:
    id: int = 100
    type: str = "private"


@dataclass
class _FakeUser:
    id: int = 7
    username: str | None = "alice"
    first_name: str | None = "Alice"


def _make_update(
    *,
    text: str = "",
    chat: _FakeChat | None = None,
    user: _FakeUser | None = None,
) -> MagicMock:
    """Build a fake telegram.Update with the attribute slice the handlers need."""
    chat = chat or _FakeChat()
    user = user or _FakeUser()
    msg = MagicMock()
    msg.text = text
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = chat
    update.effective_user = user
    return update


def _make_channel(workspace: Path, *, admin_user_ids: list[int] | None = None) -> MagicMock:
    """Build a fake TelegramChannel with the attribute slice the handlers need."""
    channel = MagicMock()
    channel.name = "telegram"
    channel.workspace = workspace
    channel.bus = MessageBus()
    channel.config = MagicMock()
    channel.config.admin_user_ids = admin_user_ids or []
    channel.is_admin = lambda uid: uid in (admin_user_ids or [])
    channel.addr = lambda chat_id: MessageAddress("telegram", str(chat_id))
    states: dict[str, _FakeUserState] = {}

    def _user_state(chat_id: int) -> _FakeUserState:
        return states.setdefault(str(chat_id), _FakeUserState())

    channel.user_state = _user_state
    return channel


def _seed_personalities(workspace: Path) -> None:
    (workspace / "personalities.yaml").write_text(
        "personalities:\n"
        '  - name: default\n    label: Default\n    description: Neutral.\n    overlay: ""\n'
        "  - name: vc_partner\n    label: VC Partner\n    description: VC.\n"
        "    overlay: |\n      Adopt the voice of a Series-B VC partner.\n",
        encoding="utf-8",
    )
    personalities._CACHE.pop(workspace, None)


# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_clear_publishes_reset_event_in_dm(tmp_path: Path):
    channel = _make_channel(tmp_path)
    addr = channel.addr(100)
    storage_layout.ensure_user_dirs(tmp_path, addr)
    auth_module.write_secret(tmp_path, "ABCDEF")
    auth_module.write_marker(tmp_path, addr, "ABCDEF")
    update = _make_update(chat=_FakeChat(id=100, type="private"))
    # Pre-load some state to verify it gets cleared.
    state = channel.user_state(100)
    state.replies["m1"] = "old"
    state.seen_first_citation = True
    state.served_reactions.add("👍")

    await cmd_module.cmd_clear(channel, update, MagicMock())

    event = await channel.bus.consume_inbound(address=addr)
    assert isinstance(event, SessionControlEvent)
    assert event.action == "reset"
    assert state.replies == {}
    assert state.seen_first_citation is False
    assert state.served_reactions == set()
    update.effective_message.reply_text.assert_awaited_once_with("Conversation cleared.")


@pytest.mark.asyncio
async def test_cmd_clear_blocked_when_unauthenticated(tmp_path: Path):
    """Gate sends a nudge in DMs and returns False before the handler runs."""
    channel = _make_channel(tmp_path)
    update = _make_update(chat=_FakeChat(id=100, type="private"))
    update.effective_chat.send_message = AsyncMock()  # gate calls this on unauth DM

    await cmd_module.cmd_clear(channel, update, MagicMock())

    # No bus event published.
    assert not channel.bus.inbound
    update.effective_message.reply_text.assert_not_called()


# ---------------------------------------------------------------------------
# /forgetme
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_forgetme_publishes_forget_event_dm_message(tmp_path: Path):
    channel = _make_channel(tmp_path)
    addr = channel.addr(100)
    storage_layout.ensure_user_dirs(tmp_path, addr)
    update = _make_update(chat=_FakeChat(id=100, type="private"))

    await cmd_module.cmd_forgetme(channel, update, MagicMock())

    event = await channel.bus.consume_inbound(address=addr)
    assert isinstance(event, SessionControlEvent)
    assert event.action == "forget"
    update.effective_message.reply_text.assert_awaited_once()
    reply = update.effective_message.reply_text.call_args.args[0]
    assert "Your storage has been deleted" in reply


@pytest.mark.asyncio
async def test_cmd_forgetme_works_without_prior_auth(tmp_path: Path):
    """forgetme allows allow_unauth=True so a user can wipe state even from
    a half-authed-then-rotated state."""
    channel = _make_channel(tmp_path)
    update = _make_update(chat=_FakeChat(id=100, type="private"))

    await cmd_module.cmd_forgetme(channel, update, MagicMock())

    addr = channel.addr(100)
    event = await channel.bus.consume_inbound(address=addr)
    assert isinstance(event, SessionControlEvent)
    assert event.action == "forget"


# ---------------------------------------------------------------------------
# /setsecret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_setsecret_admin_with_code_writes_secret(tmp_path: Path):
    channel = _make_channel(tmp_path, admin_user_ids=[7])
    update = _make_update(text="/setsecret ABCDEF", user=_FakeUser(id=7))

    await cmd_module.cmd_setsecret(channel, update, MagicMock())

    record = auth_module.read_secret(tmp_path)
    assert record is not None
    assert record.code == "ABCDEF"
    update.effective_message.reply_text.assert_awaited_once()
    assert "ABCDEF" in update.effective_message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_cmd_setsecret_admin_no_code_generates_one(tmp_path: Path):
    channel = _make_channel(tmp_path, admin_user_ids=[7])
    update = _make_update(text="/setsecret", user=_FakeUser(id=7))

    await cmd_module.cmd_setsecret(channel, update, MagicMock())

    record = auth_module.read_secret(tmp_path)
    assert record is not None
    assert auth_module.is_valid_code_shape(record.code)


@pytest.mark.asyncio
async def test_cmd_setsecret_rejects_invalid_alphabet(tmp_path: Path):
    """Codes outside SECRET_ALPHABET must be rejected without writing."""
    channel = _make_channel(tmp_path, admin_user_ids=[7])
    update = _make_update(text="/setsecret oops0", user=_FakeUser(id=7))

    await cmd_module.cmd_setsecret(channel, update, MagicMock())

    assert auth_module.read_secret(tmp_path) is None
    reply = update.effective_message.reply_text.call_args.args[0]
    assert "alphabet" in reply.lower()


@pytest.mark.asyncio
async def test_cmd_setsecret_blocks_non_admin(tmp_path: Path):
    channel = _make_channel(tmp_path, admin_user_ids=[7])
    update = _make_update(text="/setsecret ABCDEF", user=_FakeUser(id=99))

    await cmd_module.cmd_setsecret(channel, update, MagicMock())

    assert auth_module.read_secret(tmp_path) is None
    update.effective_message.reply_text.assert_awaited_once_with("Admin command.")


# ---------------------------------------------------------------------------
# /whoauthed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_whoauthed_lists_authed_addresses(tmp_path: Path):
    channel = _make_channel(tmp_path, admin_user_ids=[7])
    auth_module.write_secret(tmp_path, "ABCDEF")
    addr_a = MessageAddress("telegram", "100")
    addr_b = MessageAddress("telegram", "200")
    storage_layout.ensure_user_dirs(tmp_path, addr_a)
    storage_layout.ensure_user_dirs(tmp_path, addr_b)
    auth_module.write_marker(tmp_path, addr_a, "ABCDEF")
    auth_module.write_marker(tmp_path, addr_b, "ABCDEF")

    update = _make_update(user=_FakeUser(id=7))
    await cmd_module.cmd_whoauthed(channel, update, MagicMock())

    reply = update.effective_message.reply_text.call_args.args[0]
    assert "Authenticated (2):" in reply
    assert "100" in reply and "200" in reply


@pytest.mark.asyncio
async def test_cmd_whoauthed_says_none_when_empty(tmp_path: Path):
    channel = _make_channel(tmp_path, admin_user_ids=[7])
    update = _make_update(user=_FakeUser(id=7))

    await cmd_module.cmd_whoauthed(channel, update, MagicMock())

    update.effective_message.reply_text.assert_awaited_once_with("No authenticated users.")


@pytest.mark.asyncio
async def test_cmd_whoauthed_blocks_non_admin(tmp_path: Path):
    channel = _make_channel(tmp_path, admin_user_ids=[7])
    update = _make_update(user=_FakeUser(id=99))

    await cmd_module.cmd_whoauthed(channel, update, MagicMock())

    update.effective_message.reply_text.assert_awaited_once_with("Admin command.")


# ---------------------------------------------------------------------------
# /personality
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_personality_sets_named_persona_in_dm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _seed_personalities(tmp_path)
    channel = _make_channel(tmp_path)
    addr = channel.addr(100)
    storage_layout.ensure_user_dirs(tmp_path, addr)
    auth_module.write_secret(tmp_path, "ABCDEF")
    auth_module.write_marker(tmp_path, addr, "ABCDEF")
    # Suppress the actual outbound publish; we don't model it here.
    monkeypatch.setattr(cmd_module, "announce_persona_switch", AsyncMock())

    update = _make_update(text="/personality vc_partner", chat=_FakeChat(id=100, type="private"))
    await cmd_module.cmd_personality(channel, update, MagicMock())

    chosen = personalities.read_personality(tmp_path, addr)
    assert chosen.name == "vc_partner"
    reply = update.effective_message.reply_text.call_args.args[0]
    assert "VC Partner" in reply


@pytest.mark.asyncio
async def test_cmd_personality_rejects_unknown_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _seed_personalities(tmp_path)
    channel = _make_channel(tmp_path)
    addr = channel.addr(100)
    storage_layout.ensure_user_dirs(tmp_path, addr)
    auth_module.write_secret(tmp_path, "ABCDEF")
    auth_module.write_marker(tmp_path, addr, "ABCDEF")
    monkeypatch.setattr(cmd_module, "announce_persona_switch", AsyncMock())

    update = _make_update(text="/personality nonexistent", chat=_FakeChat(id=100, type="private"))
    await cmd_module.cmd_personality(channel, update, MagicMock())

    reply = update.effective_message.reply_text.call_args.args[0]
    assert "Unknown personality" in reply


@pytest.mark.asyncio
async def test_cmd_personality_no_arg_shows_keyboard(
    tmp_path: Path,
):
    _seed_personalities(tmp_path)
    channel = _make_channel(tmp_path)
    addr = channel.addr(100)
    storage_layout.ensure_user_dirs(tmp_path, addr)
    auth_module.write_secret(tmp_path, "ABCDEF")
    auth_module.write_marker(tmp_path, addr, "ABCDEF")

    update = _make_update(text="/personality", chat=_FakeChat(id=100, type="private"))
    await cmd_module.cmd_personality(channel, update, MagicMock())

    # Reply text is the prompt; keyboard goes in reply_markup.
    call = update.effective_message.reply_text.call_args
    assert call.args[0] == "Choose a persona:"
    assert call.kwargs.get("reply_markup") is not None
