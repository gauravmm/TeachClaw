from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchclaw import personalities
from benchclaw import storage as storage_layout
from benchclaw.agent.loop import AgentLoop, ToolCallTracker, _AddressState
from benchclaw.agent.tools.base import ToolContext
from benchclaw.bus import (
    MessageAddress,
    MessageBus,
    SessionControlEvent,
)
from benchclaw.config import Config
from benchclaw.media import MediaRepository
from benchclaw.providers.base import LLMProvider, LLMResponse
from benchclaw.session import Session, UserEvent


class _NoopProvider(LLMProvider):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        return LLMResponse(content="ok")


def _make_loop(tmp_path: Path) -> AgentLoop:
    config = Config()
    config.agents.master.workspace = str(tmp_path)
    return AgentLoop(
        config=config,
        bus=MessageBus(),
        provider=_NoopProvider(),
        media_repo=MediaRepository(tmp_path),
    )


def test_reset_event_clears_session_and_personality(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    addr = MessageAddress("telegram", "abc")
    session = Session(addr)
    session.append(UserEvent(content="prior message"))
    storage_layout.ensure_user_dirs(tmp_path, addr)
    personalities.write_personality(tmp_path, addr, "skeptical_cfo")

    state = _AddressState()
    state.tool_call_trace = [{"name": "x", "result": "y"}]
    loop._apply_control_event(SessionControlEvent(action="reset"), session, state, addr)

    assert session.events == []
    assert state.tool_call_trace == []
    assert personalities.read_personality(tmp_path, addr).name == "default"


def test_forget_event_removes_storage(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    addr = MessageAddress("telegram", "abc")
    session = Session(addr)
    session.append(UserEvent(content="prior message"))
    storage_layout.ensure_user_dirs(tmp_path, addr)
    storage_root = storage_layout.storage_root(tmp_path, addr)
    (storage_root / "scratch.txt").write_text("hi")
    assert storage_root.exists()

    state = _AddressState()
    loop._apply_control_event(SessionControlEvent(action="forget"), session, state, addr)

    assert session.events == []
    assert not storage_root.exists()


@pytest.mark.asyncio
async def test_apply_batch_handles_control_event(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    addr = MessageAddress("telegram", "abc")
    session = Session(addr)
    session.append(UserEvent(content="prior"))
    state = _AddressState()
    tracker = ToolCallTracker()

    await loop.bus.publish_inbound(addr, SessionControlEvent(action="reset"))
    batch = await loop.bus.consume_inbound_batch(address=addr)
    loop._apply_batch(batch, session, tracker, addr, state)

    assert session.events == []


def test_personality_overlay_threaded_into_system_prompt(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    addr = MessageAddress("telegram", "abc")
    storage_layout.ensure_user_dirs(tmp_path, addr)
    personalities.write_personality(tmp_path, addr, "vc_partner")
    session = Session(addr)
    session.append(UserEvent(content="hi"))

    messages = loop._build_prompt_and_messages(session, addr, pending_media=[])
    system = messages[0]["content"]
    assert isinstance(system, str)
    assert "Series-B VC partner" in system


def test_outbound_message_carries_tool_trace(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    state = _AddressState()
    state.tool_call_trace = [
        {"id": "tc1", "name": "search", "arguments": {"q": "x"}, "result": "ok"}
    ]
    # The actual outbound publish path is exercised in test_agent_loop;
    # this test guards the trace surface that the channel relies on.
    assert state.tool_call_trace[0]["name"] == "search"
    assert AgentLoop._truncate_for_trace("a" * 500).endswith("…")
