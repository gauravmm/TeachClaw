from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchclaw.agent.loop import (
    _CITATION_MAX_RETRIES,
    AgentLoop,
    ToolCallTracker,
    _AddressState,
)
from benchclaw.agent.tools.base import ToolContext
from benchclaw.bus import (
    InboundMessage,
    MessageAddress,
    MessageBus,
    OutboundMessage,
)
from benchclaw.config import Config
from benchclaw.media import MediaRepository
from benchclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from benchclaw.session import (
    AssistantEvent,
    RenderOptions,
    Session,
    SummaryEvent,
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
        top_p: float | None = None,
        top_k: int | None = None,
        enable_thinking: bool | None = None,
    ) -> LLMResponse:
        return self._response


class _ScriptedProvider(LLMProvider):
    """Provider that returns scripted responses in order, recording each call."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        top_p: float | None = None,
        top_k: int | None = None,
        enable_thinking: bool | None = None,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "model": model})
        return self._responses.pop(0)


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
            state=_AddressState(),
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
            state=_AddressState(),
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

    messages = session.render_llm_messages("system prompt", RenderOptions())
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


@pytest.mark.asyncio
async def test_proactive_compaction_summarizes_when_estimate_exceeds_threshold(
    tmp_path: Path,
) -> None:
    config = Config()
    config.agents.master.workspace = str(tmp_path)
    config.agents.master.context_window = 200
    config.agents.master.max_tokens = 50
    config.agents.master.compaction.threshold = 0.5

    provider = _ScriptedProvider(
        [
            LLMResponse(content="SUMMARY OF PRIOR CHAT"),
            LLMResponse(content="OK, here is the answer."),
        ]
    )
    loop = AgentLoop(
        config=config,
        bus=MessageBus(),
        provider=provider,
        media_repo=MediaRepository(tmp_path),
    )
    addr = MessageAddress("telegram", "123")
    session = Session(addr)
    for i in range(6):
        session.append(UserEvent(content=f"old user msg {i} " + "x" * 200))
        session.append(AssistantEvent(content=f"old assistant reply {i} " + "y" * 200))
    session.append(UserEvent(content="latest question"))
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
            state=_AddressState(),
        )

    assert len(provider.calls) == 2, "expected one summarize call + one main call"
    summary_call = provider.calls[0]
    main_call = provider.calls[1]
    assert summary_call["tools"] is None, "summarizer must not see the agent toolset"
    assert any(
        isinstance(m.get("content"), str) and "summary" in m["content"].lower()
        for m in summary_call["messages"]
    ), "summarize prompt missing"
    assert isinstance(session.events[0], SummaryEvent)
    assert session.events[0].content == "SUMMARY OF PRIOR CHAT"
    assert isinstance(session.events[1], UserEvent)
    assert session.events[1].content == "latest question"
    assert isinstance(session.events[-1], AssistantEvent)
    assert session.events[-1].content == "OK, here is the answer."
    main_messages = main_call["messages"]
    assert main_messages[0]["role"] == "system"
    assert any(
        m["role"] == "user" and "latest question" in str(m.get("content", ""))
        for m in main_messages
    ), "latest user message must be visible verbatim to the main call"


@pytest.mark.asyncio
async def test_no_compaction_when_under_threshold(tmp_path: Path) -> None:
    config = Config()
    config.agents.master.workspace = str(tmp_path)
    config.agents.master.context_window = 100000
    config.agents.master.max_tokens = 4096

    provider = _ScriptedProvider([LLMResponse(content="hello back")])
    loop = AgentLoop(
        config=config,
        bus=MessageBus(),
        provider=provider,
        media_repo=MediaRepository(tmp_path),
    )
    addr = MessageAddress("telegram", "123")
    session = Session(addr)
    session.append(UserEvent(content="hi"))
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
            state=_AddressState(),
        )

    assert len(provider.calls) == 1, "no summarization expected when under threshold"
    assert all(not isinstance(e, SummaryEvent) for e in session.events)


def test_render_options_elide_replaces_old_retrieval_results() -> None:
    addr = MessageAddress("telegram", "1")
    session = Session(addr=addr)
    session.append(UserEvent(content="first question"))
    session.append(
        ToolEvent(
            tool_call_id="tc1",
            tool_name="search",
            content="big chunk body 1",
        )
    )
    session.append(AssistantEvent(content="answered first"))
    session.append(UserEvent(content="second question"))
    session.append(
        ToolEvent(
            tool_call_id="tc2",
            tool_name="search",
            content="big chunk body 2",
        )
    )

    rendered = session.render_llm_messages(
        "system",
        options=RenderOptions(elide_tool_names=("search",)),
    )

    tool_messages = [m for m in rendered if m["role"] == "tool"]
    assert len(tool_messages) == 2
    assert "elided" in tool_messages[0]["content"]
    assert tool_messages[1]["content"] == "big chunk body 2"


def _kb_events(*ids: str) -> list[ToolEvent]:
    return [
        ToolEvent(
            tool_call_id="tc-0",
            tool_name="kb__search",
            content="\n".join(f'{{"id": "{cid}"}}' for cid in ids),
        )
    ]


def test_validate_citations_passes_when_all_ids_valid() -> None:
    events = _kb_events("a", "b")
    content = 'See <citation id="a">claim one</citation> and <citation id="b">claim two</citation>.'
    bad_ids, bad_refs = AgentLoop._validate_citations(content, events)
    assert bad_ids == []
    assert bad_refs == []


def test_validate_citations_flags_unknown_ids_with_indexed_refs() -> None:
    events = _kb_events("a", "b")
    # First citation valid, second invalid → bad_ref points at [2].
    content = 'See <citation id="a">good</citation> and <citation id="ghost">made up</citation>.'
    bad_ids, bad_refs = AgentLoop._validate_citations(content, events)
    assert bad_ids == ["ghost"]
    assert bad_refs == [2]


def test_validate_citations_with_no_kb_calls_marks_everything_bad() -> None:
    content = 'Citing <citation id="x">x</citation>.'
    bad_ids, bad_refs = AgentLoop._validate_citations(content, [])
    assert bad_ids == ["x"]
    assert bad_refs == [1]


def test_corpora_in_kb_result_picks_out_distinct_tags() -> None:
    text = (
        '{"id": "a", "corpus": "consulting", "title": "x"}\n'
        '{"id": "b", "corpus": "memes", "title": "y.jpg"}\n'
        '{"id": "c", "corpus": "consulting", "title": "z"}'
    )
    assert AgentLoop._corpora_in_kb_result(text) == {"consulting", "memes"}


def test_corpora_in_kb_result_returns_empty_on_unparseable() -> None:
    assert AgentLoop._corpora_in_kb_result("not json") == set()
    assert AgentLoop._corpora_in_kb_result(["list", "not", "string"]) == set()


def test_validate_citations_accepts_ids_from_earlier_turn() -> None:
    # A kb__search ran in turn 1; turn 2 has a new user message and the
    # current-turn trace would be empty. Validation must walk the full
    # session, not just the current turn, so the prior id stays valid.
    events: list = [
        UserEvent(content="turn 1"),
        ToolEvent(
            tool_call_id="tc1",
            tool_name="kb__search",
            content='{"id": "page-028"}',
        ),
        AssistantEvent(content="answered turn 1"),
        UserEvent(content="turn 2 follow-up"),
    ]
    content = '<citation id="page-028">prior fact</citation>.'
    bad_ids, _ = AgentLoop._validate_citations(content, events)
    assert bad_ids == []


def test_append_unverified_postscript_singular_and_plural() -> None:
    one = AgentLoop._append_unverified_postscript("Hello.", [3])
    assert one.endswith("_Citation [3] is not automatically verifiable. Check claims carefully._")
    many = AgentLoop._append_unverified_postscript("Hello.", [2, 5])
    assert many.endswith(
        "_Citations [2], [5] are not automatically verifiable. Check claims carefully._"
    )


@pytest.mark.asyncio
async def test_invalid_citation_pushback_keeps_typing_indicator_on(tmp_path: Path) -> None:
    """A pushback retry sets expecting_followup_turn so the address loop
    skips its top-of-loop typing=False publish, leaving the bubble on
    until the second LLM call completes."""
    bad_response = LLMResponse(content='Answer <citation id="ghost">claim</citation>.')
    loop = _make_loop(tmp_path, bad_response)
    addr = MessageAddress("telegram", "1")
    session = Session(addr)
    session.append(UserEvent(content="hi"))
    tracker = ToolCallTracker()
    state = _AddressState()

    async with loop.tools:
        call_ctx = ToolContext(
            workspace=loop.tools._master_ctx.workspace,
            bus=loop.bus,
            media_repo=loop.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
        )
        await loop._apply_llm_response(bad_response, session, tracker, call_ctx, addr, state)

    assert state.expecting_followup_turn is True


@pytest.mark.asyncio
async def test_invalid_citation_triggers_retry_then_postscript(tmp_path: Path) -> None:
    """Bad citation → one retry → second bad reply ships with postscript."""
    bad_response = LLMResponse(content='Answer <citation id="ghost">claim</citation>.')
    loop = _make_loop(tmp_path, bad_response)
    addr = MessageAddress("telegram", "1")
    session = Session(addr)
    session.append(UserEvent(content="hi"))
    # Seed a kb result in session history so the validator has something
    # legitimate to compare "ghost" against.
    session.append(
        ToolEvent(
            tool_call_id="tc1",
            tool_name="kb__search",
            content='{"id": "real"}',
        )
    )
    tracker = ToolCallTracker()
    state = _AddressState()

    async with loop.tools:
        call_ctx = ToolContext(
            workspace=loop.tools._master_ctx.workspace,
            bus=loop.bus,
            media_repo=loop.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
        )
        # First pass: bad citation → retry, no outbound, reminder injected.
        await loop._apply_llm_response(bad_response, session, tracker, call_ctx, addr, state)
        assert state.citation_retries == 1
        assert loop.bus.outbound.get("telegram") is None or loop.bus.outbound["telegram"].empty()
        # Inbound queue should now hold the SystemMessageEvent reminder.
        assert addr in loop.bus.inbound and not loop.bus.inbound[addr].empty()

        # Second pass: still bad → publish anyway with postscript.
        await loop._apply_llm_response(bad_response, session, tracker, call_ctx, addr, state)
        assert state.citation_retries == _CITATION_MAX_RETRIES
        outbound_q = loop.bus.outbound["telegram"]
        published = outbound_q.get_nowait()
        assert isinstance(published, OutboundMessage)
        assert "not automatically verifiable" in published.content
        assert "[1]" in published.content
