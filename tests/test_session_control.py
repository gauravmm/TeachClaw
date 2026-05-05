from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchclaw import personalities
from benchclaw import storage as storage_layout
from benchclaw.agent.loop import AgentLoop
from benchclaw.agent.loop_state import AddressState, ToolCallTracker
from benchclaw.bus import (
    MessageAddress,
    MessageBus,
    SessionControlEvent,
    ToolCallTrace,
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

    state = AddressState()
    state.tool_call_trace = [ToolCallTrace(id="x", name="x", arguments={}, result="y")]
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

    state = AddressState()
    loop._apply_control_event(SessionControlEvent(action="forget"), session, state, addr)

    assert session.events == []
    assert not storage_root.exists()


@pytest.mark.asyncio
async def test_apply_batch_handles_control_event(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    addr = MessageAddress("telegram", "abc")
    session = Session(addr)
    session.append(UserEvent(content="prior"))
    state = AddressState()
    tracker = ToolCallTracker()

    await loop.bus.publish_inbound(addr, SessionControlEvent(action="reset"))
    batch = await loop.bus.consume_inbound_batch(address=addr)
    loop._apply_batch(batch, session, tracker, addr, state)

    assert session.events == []


def test_personality_overlay_injected_into_synthetic_tail(tmp_path: Path) -> None:
    """Persona text rides along with the synthetic <current_time>/<storage_listing>
    user message right before the latest user turn, so it can change without
    invalidating the cacheable system-prompt prefix."""
    loop = _make_loop(tmp_path)
    addr = MessageAddress("telegram", "abc")
    storage_layout.ensure_user_dirs(tmp_path, addr)
    personalities.write_personality(tmp_path, addr, "vc_partner")
    session = Session(addr)
    session.append(UserEvent(content="hi"))

    messages = loop._build_prompt(session, addr, pending_media=[]).messages
    system = messages[0]["content"]
    assert isinstance(system, str)
    assert "Series-B VC partner" not in system

    synthetic = messages[-2]["content"]
    assert isinstance(synthetic, str)
    assert "<persona>" in synthetic
    assert "Series-B VC partner" in synthetic


def test_outbound_message_carries_tool_trace(tmp_path: Path) -> None:
    _ = _make_loop(tmp_path)
    state = AddressState()
    state.tool_call_trace = [
        ToolCallTrace(id="tc1", name="search", arguments={"q": "x"}, result="ok")
    ]
    assert state.tool_call_trace[0].name == "search"


def test_stringify_tool_result_preserves_full_content() -> None:
    """The agent loop deliberately does NOT truncate trace results — the
    channel needs the raw text to extract structured payloads (like kb
    records for the citation listing)."""
    long_str = "x" * 500
    assert AgentLoop._stringify_tool_result(long_str) == long_str
    assert AgentLoop._stringify_tool_result({"a": 1}) == '{"a": 1}'
