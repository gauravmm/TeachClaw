from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchclaw.agent.loop import AgentLoop, ToolCallTracker
from benchclaw.agent.tools.base import ToolContext
from benchclaw.bus import InboundMessage, MessageAddress, MessageBus, OutboundMessage
from benchclaw.config import Config
from benchclaw.media import MediaRepository
from benchclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from benchclaw.session import (
    AssistantEvent,
    RenderOptions,
    Session,
    SystemEvent,
    ToolEvent,
    UserEvent,
)


class _FakeProvider(LLMProvider):
    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        return self._response


def _make_loop(tmp_path: Path, response: LLMResponse) -> AgentLoop:
    config = Config()
    config.agents.master.workspace = str(tmp_path)
    return AgentLoop(
        config=config,
        bus=MessageBus(),
        provider=_FakeProvider(response),
        media_repo=MediaRepository(tmp_path),
    )


@pytest.mark.asyncio
async def test_process_llm_turn_sends_visible_response(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, LLMResponse(content="Status update for you."))
    addr = MessageAddress("telegram", "123")
    session = Session(addr)
    session.append(UserEvent(content="What is the order status?"))
    tracker = ToolCallTracker()

    async with loop.tools:
        call_ctx = ToolContext(
            workspace=loop.tools._master_ctx.workspace,
            bus=loop.bus,
            media_repo=loop.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
        )
        await loop._process_llm_turn(
            session=session,
            tracker=tracker,
            call_ctx=call_ctx,
            addr=addr,
        )
        outbound = await loop.bus.consume_outbound(channel="telegram")

    assert isinstance(outbound, OutboundMessage)
    assert outbound.content == "Status update for you."
    assert isinstance(session.events[-1], AssistantEvent)
    assert session.events[-1].content == "Status update for you."


@pytest.mark.asyncio
async def test_process_llm_turn_records_tool_calls_as_events(tmp_path: Path) -> None:
    loop = _make_loop(
        tmp_path,
        LLMResponse(
            content="Checking that now.",
            tool_calls=[
                ToolCallRequest(
                    id="tc1",
                    name="write_file",
                    arguments={"path": "note.md", "content": "step"},
                )
            ],
        ),
    )
    addr = MessageAddress("telegram", "123")
    session = Session(addr)
    session.append(UserEvent(content="Do the thing"))
    tracker = ToolCallTracker()

    async with loop.tools:
        call_ctx = ToolContext(
            workspace=loop.tools._master_ctx.workspace,
            bus=loop.bus,
            media_repo=loop.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
        )
        await loop._process_llm_turn(
            session=session,
            tracker=tracker,
            call_ctx=call_ctx,
            addr=addr,
        )
        outbound = await loop.bus.consume_outbound(channel="telegram")

    assert isinstance(outbound, OutboundMessage)
    assert outbound.content == "Checking that now."
    assert isinstance(session.events[-1], AssistantEvent)
    assert session.events[-1].tool_calls is not None
    assert session.events[-1].tool_calls[0]["function"]["name"] == "write_file"
    assert tracker.pending


def test_tool_call_tracker_interrupt_records_background_notice() -> None:
    session = Session(MessageAddress("telegram", "123"))
    tracker = ToolCallTracker()
    tracker.add("tc1", "web_search", None)  # type: ignore[arg-type]

    tracker.handle_interrupt(session)

    assert not tracker.pending
    assert isinstance(session.events[-1], SystemEvent)
    assert "still executing in the background" in str(session.events[-1].content)


def test_build_llm_messages_keeps_only_latest_reasoning(tmp_path: Path) -> None:
    addr = MessageAddress("telegram", "123")
    session = Session(addr)
    session.append(UserEvent(content="hi"))
    session.append(AssistantEvent(content="first", reasoning_content="older reasoning"))
    session.append(AssistantEvent(content="second", reasoning_content="x" * 600))

    messages = session.render_llm_messages(
        "system prompt",
        MediaRepository(tmp_path),
        RenderOptions(),
    )
    assistant_messages = [message for message in messages if message["role"] == "assistant"]

    assert "reasoning_content" not in assistant_messages[0]
    assert assistant_messages[1]["reasoning_content"] == ("x" * 500) + " [truncated]"


def test_build_llm_messages_redacts_image_blocks_in_debug_profile(tmp_path: Path) -> None:
    addr = MessageAddress("telegram", "123")
    session = Session(addr)
    session.append(
        ToolEvent(
            content=[
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + ("a" * 80)}}
            ],
            tool_call_id="tc1",
            tool_name="read_image",
        )
    )

    messages = session.render_llm_messages(
        "system prompt",
        MediaRepository(tmp_path),
        RenderOptions(max_inline_image_url_chars=40),
    )
    tool_message = next(message for message in messages if message["role"] == "tool")

    assert tool_message["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aaaaaaaaaaaaaaaaaa…"}}
    ]


def test_render_llm_messages_keeps_full_image_blocks_for_provider(tmp_path: Path) -> None:
    addr = MessageAddress("telegram", "123")
    session = Session(addr)
    session.append(
        ToolEvent(
            content=[
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + ("a" * 80)}}
            ],
            tool_call_id="tc1",
            tool_name="read_image",
        )
    )

    messages = session.render_llm_messages(
        "system prompt", MediaRepository(tmp_path), RenderOptions()
    )
    tool_message = next(message for message in messages if message["role"] == "tool")

    assert tool_message["content"] == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + ("a" * 80)},
        }
    ]


def test_collapse_user_messages_returns_one_user_event() -> None:
    addr = MessageAddress("telegram", "123")
    messages = [
        InboundMessage(
            address=addr,
            sender_id="alice",
            content="first",
            media=["a.png"],
            media_metadata=[],
            metadata={"sender_label": "Alice"},
        ),
        InboundMessage(
            address=addr,
            sender_id="bob",
            content="second",
            media=["b.png"],
            media_metadata=[],
            metadata={"sender_label": "Bob"},
        ),
    ]

    event = AgentLoop._collapse_user_messages(messages)

    assert isinstance(event, UserEvent)
    assert event.content == "[alice] first\n[bob] second"
    assert event.media == ["a.png", "b.png"]
