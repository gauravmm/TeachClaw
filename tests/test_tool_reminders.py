from __future__ import annotations

import asyncio

import pytest

from teachclaw.agent.loop_state import ToolCallTracker
from teachclaw.bus import MessageAddress, ToolResultEvent
from teachclaw.config import Config, ToolReminder
from teachclaw.session import (
    Session,
    SystemEvent,
    ToolEvent,
    UserEvent,
    event_from_record,
)


def _result(name: str, result: str = "ok") -> ToolResultEvent:
    return ToolResultEvent(tool_call_id=f"call-{name}", tool_name=name, result=result)


def _dispatched_tracker(reminders: dict[str, ToolReminder], names: list[str]) -> ToolCallTracker:
    """Build a tracker with each name pre-registered as if it had been dispatched
    by ResponseHandler — so handle_result takes the in-flight (foreground) path
    rather than the cron/background-completion path that emits its own nudge."""
    tracker = ToolCallTracker(reminders)
    for name in names:

        async def _noop() -> None:
            return None

        tracker.add(f"call-{name}", name, asyncio.get_event_loop().create_task(_noop()))
    return tracker


@pytest.mark.asyncio
async def test_tracker_appends_reminder_for_configured_tool() -> None:
    addr = MessageAddress("telegram", "x")
    session = Session(addr)
    tracker = _dispatched_tracker(
        {"kb__search": ToolReminder(text="cite your sources", ephemeral=False)},
        ["kb__search"],
    )

    tracker.handle_result(_result("kb__search"), session)

    kinds = [type(e).__name__ for e in session.events]
    assert kinds == ["ToolEvent", "SystemEvent"]
    reminder = session.events[-1]
    assert isinstance(reminder, SystemEvent)
    assert reminder.content == "cite your sources"
    assert reminder.ephemeral is False


@pytest.mark.asyncio
async def test_tracker_skips_reminder_for_unconfigured_tool() -> None:
    addr = MessageAddress("telegram", "x")
    session = Session(addr)
    tracker = _dispatched_tracker({"kb__search": ToolReminder(text="x")}, ["read_file"])

    tracker.handle_result(_result("read_file"), session)

    assert [type(e).__name__ for e in session.events] == ["ToolEvent"]


@pytest.mark.asyncio
async def test_tracker_appends_ephemeral_reminder() -> None:
    addr = MessageAddress("telegram", "x")
    session = Session(addr)
    tracker = _dispatched_tracker(
        {"cute-db__search_cute": ToolReminder(text="call send_media", ephemeral=True)},
        ["cute-db__search_cute"],
    )

    tracker.handle_result(_result("cute-db__search_cute"), session)

    reminder = session.events[-1]
    assert isinstance(reminder, SystemEvent)
    assert reminder.ephemeral is True


@pytest.mark.asyncio
async def test_tracker_appends_reminder_before_background_completion_nudge() -> None:
    """Cron-style result (no prior tracker.add) emits both the configured
    reminder AND the existing background-completion nudge. Reminder must come
    first so it sits adjacent to the ToolEvent it is about."""
    addr = MessageAddress("telegram", "x")
    session = Session(addr)
    tracker = ToolCallTracker({"cron_fired": ToolReminder(text="reminder-text", ephemeral=False)})

    tracker.handle_result(_result("cron_fired"), session)

    kinds = [type(e).__name__ for e in session.events]
    assert kinds == ["ToolEvent", "SystemEvent", "SystemEvent"]
    assert session.events[1].content == "reminder-text"
    assert "Background tool" in session.events[2].content


def test_render_hides_ephemeral_after_next_user_event() -> None:
    addr = MessageAddress("telegram", "x")
    session = Session(addr)
    session.append(UserEvent(content="first"))
    session.append(ToolEvent(content="result", tool_call_id="c1", tool_name="cute-db__search_cute"))
    session.append(SystemEvent(content="ephemeral nudge", ephemeral=True))
    session.append(SystemEvent(content="persistent nudge", ephemeral=False))

    contents = [m.get("content") for m in session.render_history(session.events)]
    assert "<system_event>ephemeral nudge</system_event>" in contents
    assert "<system_event>persistent nudge</system_event>" in contents

    session.append(UserEvent(content="follow up"))

    contents = [m.get("content") for m in session.render_history(session.events)]
    assert "<system_event>ephemeral nudge</system_event>" not in contents
    assert "<system_event>persistent nudge</system_event>" in contents


def test_system_event_record_round_trip_preserves_ephemeral() -> None:
    persistent = SystemEvent(content="p")
    ephemeral = SystemEvent(content="e", ephemeral=True)

    p_record = persistent.to_record()
    e_record = ephemeral.to_record()

    assert "ephemeral" not in p_record
    assert e_record["ephemeral"] is True

    p_loaded = event_from_record(p_record)
    e_loaded = event_from_record(e_record)

    assert isinstance(p_loaded, SystemEvent) and p_loaded.ephemeral is False
    assert isinstance(e_loaded, SystemEvent) and e_loaded.ephemeral is True


def test_config_coerces_bare_string_reminder_to_persistent() -> None:
    config = Config.model_validate({"tool_reminders": {"foo": "bare nudge"}})

    assert config.tool_reminders["foo"].text == "bare nudge"
    assert config.tool_reminders["foo"].ephemeral is False


def test_config_accepts_dict_reminder_with_ephemeral_true() -> None:
    config = Config.model_validate({"tool_reminders": {"foo": {"text": "x", "ephemeral": True}}})

    assert config.tool_reminders["foo"].ephemeral is True


def test_config_rejects_blank_reminder_text() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Config.model_validate({"tool_reminders": {"foo": "   "}})
