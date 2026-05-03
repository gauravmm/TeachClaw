"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from benchclaw.agent.context import ContextBuilder
from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.mcp_manager import MCPManager
from benchclaw.agent.tools.registry import ToolRegistry
from benchclaw.bus import (
    InboundMessage,
    InboundMessageBatch,
    MessageAddress,
    MessageBus,
    OutboundMessage,
    SystemMessageEvent,
    ToolResultEvent,
    TypingEvent,
)
from benchclaw.config import Config
from benchclaw.media import MediaRepository
from benchclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from benchclaw import storage as storage_layout
from benchclaw.session import (
    AssistantEvent,
    ConversationEvent,
    RenderOptions,
    Session,
    SessionManager,
    SystemEvent,
    ToolEvent,
    UserEvent,
)

_SUMMARIZE_SYSTEM_PROMPT = """\
You are a conversation summarizer for an agentic AI assistant.

Compress the conversation below into a brief summary that preserves:
1. Facts the assistant has established or learned about the user or task.
2. Decisions made and the reasoning behind them.
3. Open tasks, commitments, or pending follow-ups.
4. User preferences and any context about who they are or what they want.
5. The user's most recent question or request, verbatim if short.

Drop conversational filler, repeated greetings, and routine acknowledgments.
The summary will replace the conversation in the assistant's context window,
so it must stand alone as the only record of what was discussed.
Be concise and factual. Plain prose or bulleted lists are both fine.
"""

_SUMMARIZE_MAX_TOKENS = 2048


@dataclass
class _AddressState:
    iteration_count: int = 0
    pending_system_events: list[str] = field(default_factory=list)
    pending_media: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _BatchApplication:
    needs_llm: bool = False
    start_typing: bool = False


class ToolCallTracker:
    """Per-address tracker for in-flight background tool calls."""

    def __init__(self) -> None:
        self._in_flight: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def tasks(self) -> dict[str, asyncio.Task]:
        return self._tasks

    @property
    def pending(self) -> bool:
        return bool(self._in_flight)

    def add(self, tool_call_id: str, tool_name: str, task: asyncio.Task) -> None:
        self._in_flight[tool_call_id] = tool_name
        self._tasks[tool_call_id] = task

    def handle_interrupt(self, session: Session) -> None:
        if not self._in_flight:
            return
        tool_list = ", ".join(f"{name} ({tid[:8]})" for tid, name in self._in_flight.items())
        session.append(
            SystemEvent(
                content="The following tools are still executing in the background: "
                f"{tool_list}. Their results will arrive as new events."
            )
        )
        self._in_flight.clear()

    def handle_result(self, event: ToolResultEvent, session: Session) -> bool:
        self._tasks.pop(event.tool_call_id, None)
        session.append(
            ToolEvent(
                content=event.result,
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_name,
            )
        )
        if event.tool_call_id in self._in_flight:
            del self._in_flight[event.tool_call_id]
            return not self._in_flight

        session.append(
            SystemEvent(
                content=f"Background tool '{event.tool_name}' completed. Summarize the result for the user or take any necessary follow-up actions to achieve the goal."
            )
        )
        return True


class AgentLoop:
    """Event-driven agent runtime."""

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        provider: LLMProvider,
        media_repo: MediaRepository,
        debug_dump_path: Path | None = None,
    ):
        self.workspace_path = config.workspace_path
        self.config = config.agents.master
        self.bus = bus
        self.provider = provider
        self.debug_dump_path = debug_dump_path
        self.media_repo = media_repo

        self.context = ContextBuilder(config.workspace_path)
        self.sessions = SessionManager(config.workspace_path / "sessions")

        master_ctx = ToolContext(
            workspace=config.workspace_path,
            bus=bus,
            media_repo=media_repo,
        )
        self.master_ctx = master_ctx
        mcp_manager = MCPManager(config.mcp_servers) if config.mcp_servers else None
        self.tools = ToolRegistry(config.tools, master_ctx, mcp_manager=mcp_manager)

    async def _run_tool_and_post(
        self,
        tc: ToolCallRequest,
        call_ctx: ToolContext,
        addr: MessageAddress,
    ) -> None:
        try:
            result = await self.tools.execute(tc.name, tc.arguments, call_ctx)
        except asyncio.CancelledError:
            result = "Cancelled."
        except Exception as e:
            result = f"Error executing {tc.name}: {e}"
        await self.bus.publish_inbound(
            addr,
            ToolResultEvent(tool_call_id=tc.id, tool_name=tc.name, result=result),
        )

    def _dump_messages(self, messages: list[dict[str, object]]) -> None:
        if not self.debug_dump_path:
            return

        try:
            self.debug_dump_path.write_text(
                json.dumps(messages, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to write debug dump: {e}")

    @staticmethod
    def _collapse_user_messages(messages: list[InboundMessage]) -> UserEvent:
        if len(messages) == 1:
            message = messages[0]
            return UserEvent(
                timestamp=message.timestamp,
                content=message.content,
                sender_id=message.sender_id,
                media=message.media,
                media_metadata=message.media_metadata,
                metadata=message.metadata,
            )
        parts = [f"[{m.sender_id}] {m.content}" for m in messages if m.content]
        first = messages[0]
        return UserEvent(
            timestamp=first.timestamp,
            sender_id=first.sender_id,
            content="\n".join(parts),
            media=[path for m in messages for path in m.media],
            media_metadata=[item for m in messages for item in m.media_metadata],
            metadata=first.metadata,
        )

    async def _call_provider(
        self,
        addr: MessageAddress,
        llm_messages: list[dict[str, object]],
    ):
        try:
            return await self.provider.chat(
                messages=llm_messages,
                tools=self.tools.get_definitions(),
                model=self.config.model,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        except Exception as e:
            logger.error(f"LLM error for {addr}: {e}")
            await self.bus.publish_outbound(
                OutboundMessage(address=addr, content=f"Sorry, I encountered an error: {e}")
            )
            return None

    @staticmethod
    def _estimate_tokens(messages: list[dict[str, object]]) -> int:
        """Cheap heuristic: ~4 characters per token. See spec/COMPACTION.md.

        Wide threshold margin (~18% of input budget) absorbs the heuristic's
        ~30% inaccuracy. Replace with a tokenizer-backed estimate later if
        the trigger turns out to misfire in either direction.
        """
        return len(json.dumps(messages, ensure_ascii=False)) // 4

    def _input_budget(self) -> int:
        return max(self.config.context_window - self.config.max_tokens, 1)

    def _build_render_options(
        self,
        pending_media: list[str] | None = None,
    ) -> RenderOptions:
        elide_tools: tuple[str, ...] = ()
        if self.config.compaction.elide_chunks_after_turn:
            elide_tools = tuple(self.config.compaction.elide_tool_names)
        return RenderOptions(
            pending_media_paths=pending_media or None,
            elide_tool_names=elide_tools,
        )

    def _build_prompt_and_messages(
        self,
        session: Session,
        addr: MessageAddress,
        pending_media: list[str] | None,
    ) -> list[dict[str, object]]:
        storage_root = storage_layout.storage_root(self.workspace_path, addr)
        prompt = self.context.build_system_prompt(
            self.tools.values(),
            addr.channel,
            addr.chat_id,
            session.describe_current_session(),
            chunk_elision_active=self.config.compaction.elide_chunks_after_turn,
            profile_text=storage_layout.read_profile(self.workspace_path, addr),
            storage_path=str(storage_root),
        )
        messages = session.render_llm_messages(
            prompt,
            self.media_repo,
            self._build_render_options(pending_media),
            max_messages=self.config.memory_window,
        )
        return self._inject_storage_listing(messages, addr)

    def _inject_storage_listing(
        self,
        messages: list[dict[str, object]],
        addr: MessageAddress,
    ) -> list[dict[str, object]]:
        """Insert a synthetic storage listing right before the latest user message.

        Living in the tail keeps the cacheable system-prompt prefix stable
        across writes — see spec/TODO.md storage-layout notes.
        """
        listing = storage_layout.listing_for_user(self.workspace_path, addr)
        if not listing:
            return messages
        listing_message: dict[str, object] = {
            "role": "user",
            "content": f"<storage_listing>\n{listing}\n</storage_listing>",
        }
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                return [*messages[:i], listing_message, *messages[i:]]
        return messages

    @staticmethod
    def _last_user_event_index(events: list[ConversationEvent]) -> int:
        for i in range(len(events) - 1, -1, -1):
            if isinstance(events[i], UserEvent):
                return i
        return -1

    async def _summarize_conversation(
        self,
        session: Session,
        addr: MessageAddress,
        events_to_summarize: list[ConversationEvent],
    ) -> str | None:
        """Run a separate provider call to summarize the given events.

        Returns the summary text, or None if the provider call failed.
        """
        # Render the doomed events as a one-shot conversation, swapping the
        # main system prompt for a summarization instruction. We deliberately
        # do not pass tools to this call: the summarizer is not allowed to
        # take actions, only to compress.
        history_messages = session._render_history(
            events_to_summarize,
            options=self._build_render_options(),
        )
        summarize_messages: list[dict[str, object]] = [
            {"role": "system", "content": _SUMMARIZE_SYSTEM_PROMPT},
            *history_messages,
            {
                "role": "user",
                "content": "Now produce the summary as instructed above.",
            },
        ]
        summarize_model = self.config.compaction.summarize_model or self.config.model
        try:
            response = await self.provider.chat(
                messages=summarize_messages,
                tools=None,
                model=summarize_model,
                max_tokens=_SUMMARIZE_MAX_TOKENS,
                temperature=0.3,
            )
        except Exception as e:
            logger.error(f"Summarization failed for {addr}: {e}")
            return None
        return (response.content or "").strip() or None

    async def _maybe_compact_proactive(
        self,
        session: Session,
        addr: MessageAddress,
        llm_messages: list[dict[str, object]],
    ) -> bool:
        """Estimate prompt size and summarize if over threshold.

        Returns True if compaction happened and the caller should re-render.
        """
        estimate = self._estimate_tokens(llm_messages)
        threshold_tokens = int(self.config.compaction.threshold * self._input_budget())
        if estimate <= threshold_tokens:
            return False

        last_user_idx = self._last_user_event_index(session.events)
        # Summarize everything strictly before the most recent user message; if
        # there is no prior user message (or the only user message is the very
        # first event), there is nothing useful to summarize without losing the
        # latest question, so we skip this round and let the next request
        # through unchanged.
        if last_user_idx <= 0:
            logger.warning(
                f"Session {addr} prompt {estimate} tokens > {threshold_tokens} threshold "
                f"but no compactable history before the latest user message; skipping."
            )
            return False

        to_summarize = list(session.events[:last_user_idx])
        logger.warning(
            f"Compacting session {addr}: {estimate}/{self._input_budget()} input tokens "
            f"(>{threshold_tokens} threshold); summarizing {len(to_summarize)} events."
        )
        summary = await self._summarize_conversation(session, addr, to_summarize)
        if summary is None:
            return False

        session.compact_with_summary(summary, keep_from_index=last_user_idx)
        logger.warning(
            f"Session {addr} compacted: {len(session.events)} events remain, "
            f"summary {len(summary)} chars."
        )
        return True

    async def _apply_llm_response(
        self,
        response: LLMResponse,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
    ) -> None:
        usage = response.usage
        logger.info(
            f"LLM response for {addr}: "
            f"{usage.get('prompt_tokens', '?')} prompt, "
            f"{usage.get('completion_tokens', '?')} completion, "
            f"{usage.get('total_tokens', '?')} total / {self.config.context_window} budget"
        )
        content = (response.content or "").rstrip("\n")
        if response.has_tool_calls:
            tool_call_dicts = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in response.tool_calls
            ]
            session.append(
                AssistantEvent(
                    content=content,
                    tool_calls=tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )
            )
            for tc in response.tool_calls:
                args_str = json.dumps(tc.arguments, ensure_ascii=False)
                logger.info(f"Tool call (background): {tc.name}({args_str[:200]})")
                task = asyncio.create_task(
                    self._run_tool_and_post(tc, call_ctx, addr),
                    name=f"tool-{tc.id[:8]}",
                )
                tracker.add(tc.id, tc.name, task)
            if content:
                await self.bus.publish_outbound(OutboundMessage(address=addr, content=content))
            return

        if not content:
            logger.warning(
                f"LLM returned empty response (no text, no tool calls) for {addr} — injecting nudge"
            )
            await self.bus.publish_inbound(
                addr,
                SystemMessageEvent(
                    content="You did not provide a text response. Please respond to the user now."
                ),
            )
            return
        session.append(AssistantEvent(content=content))
        preview = content[:120] + "..." if len(content) > 120 else content
        logger.info(f"Response to {addr}: {preview}")
        await self.bus.publish_outbound(OutboundMessage(address=addr, content=content))

    @staticmethod
    def _flush_pending_system_events(session: Session, state: _AddressState) -> None:
        for content in state.pending_system_events:
            session.append(SystemEvent(content=content))
        state.pending_system_events.clear()

    def _apply_batch(
        self,
        batch: InboundMessageBatch,
        session: Session,
        tracker: ToolCallTracker,
        addr: MessageAddress,
        state: _AddressState,
    ) -> _BatchApplication:
        needs_llm = False
        start_typing = False

        for result in batch.tool_results:
            tracker.handle_result(result, session)
        if batch.tool_results and not tracker.pending:
            self._flush_pending_system_events(session, state)
            needs_llm = True

        for event in batch.system_events:
            if tracker.pending:
                logger.debug(f"SystemEvent buffered (tools in flight): {event.content[:60]}")
                state.pending_system_events.append(event.content)
            else:
                session.append(SystemEvent(content=event.content))
                needs_llm = True

        if batch.user_messages:
            start_typing = True
            if tracker.pending:
                tracker.handle_interrupt(session)
            self._flush_pending_system_events(session, state)

            user_event = self._collapse_user_messages(batch.user_messages)
            preview = (
                user_event.content[:80] + "..."
                if len(user_event.content) > 80
                else user_event.content
            )
            logger.info(f"Processing message from {addr}: {preview}")
            session.append(user_event)
            state.pending_media = list(user_event.media)
            state.iteration_count = 0
            needs_llm = True

        return _BatchApplication(needs_llm=needs_llm, start_typing=start_typing)

    async def _process_llm_turn(
        self,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
        pending_media: list[str] | None = None,
    ) -> None:
        if pending_media is None:
            pending_media = []
        llm_messages = self._build_prompt_and_messages(session, addr, pending_media)
        if await self._maybe_compact_proactive(session, addr, llm_messages):
            llm_messages = self._build_prompt_and_messages(session, addr, pending_media)
        self._dump_messages(llm_messages)
        if pending_media:
            pending_media.clear()
        response = await self._call_provider(addr, llm_messages)
        if response is None:
            return
        await self._apply_llm_response(response, session, tracker, call_ctx, addr)

    async def _address_loop(self, addr: MessageAddress) -> None:
        session = self.sessions.get(addr)
        tracker = ToolCallTracker()
        storage_layout.ensure_user_dirs(self.workspace_path, addr)
        storage_root = storage_layout.storage_root(self.workspace_path, addr).resolve()
        read_roots: tuple[Path, ...] = (
            storage_layout.skills_dir(self.workspace_path).resolve(),
            storage_layout.common_dir(self.workspace_path).resolve(),
        )
        write_roots: tuple[Path, ...] = (
            storage_layout.scratch_dir(self.workspace_path, addr).resolve(),
        )
        call_ctx = ToolContext(
            workspace=self.tools._master_ctx.workspace,
            bus=self.bus,
            media_repo=self.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
            storage_root=storage_root,
            read_roots=read_roots,
            write_roots=write_roots,
        )
        state = _AddressState()

        while True:
            if not tracker.pending:
                await self.bus.publish_outbound(TypingEvent(addr, is_typing=False))

            batch = await self.bus.consume_inbound_batch(address=addr)
            batch_result = self._apply_batch(batch, session, tracker, addr, state)
            if batch_result.start_typing:
                await self.bus.publish_outbound(TypingEvent(addr, is_typing=True))
            if not batch_result.needs_llm:
                continue

            if state.iteration_count >= self.config.max_tool_iterations:
                logger.warning(f"Max tool iterations reached for {addr}")
                continue
            state.iteration_count += 1

            await self._process_llm_turn(
                session,
                tracker,
                call_ctx,
                addr,
                pending_media=state.pending_media,
            )

    async def run(self) -> None:
        async with self.sessions:
            async with self.tools:
                logger.info("Agent loop started")
                new_addr_queue = self.bus.subscribe_new_addresses()
                addr_tasks: dict[MessageAddress, asyncio.Task] = {}

                async def _dispatch() -> None:
                    while True:
                        addr = await new_addr_queue.get()
                        addr_tasks[addr] = asyncio.create_task(
                            self._address_loop(addr), name=f"agent-{addr}"
                        )

                dispatch_task = asyncio.create_task(_dispatch())
                try:
                    await asyncio.get_event_loop().create_future()
                except asyncio.CancelledError:
                    for task in [dispatch_task, *addr_tasks.values()]:
                        task.cancel()
                    await asyncio.gather(
                        dispatch_task, *addr_tasks.values(), return_exceptions=True
                    )
