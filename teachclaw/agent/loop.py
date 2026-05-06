"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from pathlib import Path

from loguru import logger

from teachclaw import personalities
from teachclaw import storage as storage_layout
from teachclaw.agent.cache_monitor import PromptCacheMonitor
from teachclaw.agent.compactor import Compactor
from teachclaw.agent.dump import dump_messages
from teachclaw.agent.loop_state import AddressState, ToolCallTracker, TurnOutcome
from teachclaw.agent.prompt import PromptBuild, PromptBuilder
from teachclaw.agent.response import (
    CITATION_REMINDER,
    CITATION_TOOL_PREFIXES,
    ResponseHandler,
    stringify_tool_result,
)
from teachclaw.agent.tools.base import ToolContext
from teachclaw.agent.tools.mcp_manager import MCPManager
from teachclaw.agent.tools.registry import ToolRegistry
from teachclaw.bus import (
    InboundMessage,
    InboundMessageBatch,
    MessageAddress,
    MessageBus,
    OutboundMessage,
    SessionControlEvent,
    TypingEvent,
)
from teachclaw.config import Config
from teachclaw.media import MediaRepository
from teachclaw.providers.base import LLMProvider
from teachclaw.session import Session, SessionManager, SystemEvent, UserEvent


class AgentLoop:
    """Event-driven agent runtime."""

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        provider: LLMProvider,
        media_repo: MediaRepository,
        debug_dump_dir: Path | None = None,
    ):
        self.workspace_path = config.workspace_path
        self.config = config.agents.master
        self.bus = bus
        self.provider = provider
        self.debug_dump_dir = debug_dump_dir
        self.media_repo = media_repo

        self.sessions = SessionManager(config.workspace_path / "sessions")

        master_ctx = ToolContext(
            workspace=config.workspace_path,
            bus=bus,
            media_repo=media_repo,
        )
        mcp_manager = MCPManager(config.mcp_servers) if config.mcp_servers else None
        self.tools = ToolRegistry(config.tools, master_ctx, mcp_manager=mcp_manager)
        self.cache_monitor = PromptCacheMonitor(log_dir=debug_dump_dir)
        self.prompt_builder = PromptBuilder(
            config.workspace_path,
            tools=self.tools,
            media_repo=media_repo,
            agent_config=self.config,
        )
        self.compactor = Compactor(provider, self.config, self.prompt_builder)
        self.response = ResponseHandler(bus, self.tools, self.config)

    @staticmethod
    def _collapse_user_messages(messages: list[InboundMessage]) -> UserEvent:
        first = messages[0]
        if len(messages) == 1:
            content = first.content
        else:
            content = "\n".join(f"[{m.sender_id}] {m.content}" for m in messages if m.content)
        return UserEvent(
            timestamp=first.timestamp,
            sender_id=first.sender_id,
            content=content,
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
                top_p=self.config.top_p,
                top_k=self.config.top_k,
                enable_thinking=self.config.enable_thinking,
            )
        except Exception as e:
            logger.error(f"LLM error for {addr}: {e}")
            await self.bus.publish_outbound(
                OutboundMessage(address=addr, content=f"Sorry, I encountered an error: {e}")
            )
            return None

    def _build_prompt(
        self,
        session: Session,
        addr: MessageAddress,
        pending_media: list[str] | None,
    ) -> PromptBuild:
        build = self.prompt_builder.build(session, addr, pending_media)
        self.cache_monitor.observe(addr, build)
        return build

    @staticmethod
    def _flush_pending_system_events(session: Session, state: AddressState) -> None:
        for content in state.pending_system_events:
            session.append(SystemEvent(content=content))
        state.pending_system_events.clear()

    @staticmethod
    def _reset_turn_state(state: AddressState) -> None:
        """Clear per-turn counters and the in-flight tool trace.

        Called whenever the session is starting fresh — new user message,
        /clear, /forgetme. ``pending_media`` is owned by the user-message
        path (it carries that turn's attachments) so it isn't cleared
        here; callers handle it explicitly.
        """
        state.tool_call_trace = []
        state.iteration_count = 0
        state.citation_retries = 0
        state.pending_system_events.clear()

    def _apply_control_event(
        self,
        event: SessionControlEvent,
        session: Session,
        state: AddressState,
        addr: MessageAddress,
    ) -> None:
        session.clear()
        self.cache_monitor.forget(addr)
        self._reset_turn_state(state)
        if event.action == "reset":
            personalities.clear_personality(self.workspace_path, addr)
            logger.info(f"Session reset for {addr}")
            return
        # action == "forget" — Literal type narrows here; pyright knows it.
        root = storage_layout.storage_root(self.workspace_path, addr)
        if root.exists():
            try:
                shutil.rmtree(root)
                logger.info(f"Storage forgotten for {addr}: removed {root}")
            except OSError as e:
                logger.error(f"Failed to remove storage for {addr}: {e}")

    async def _apply_batch(
        self,
        batch: InboundMessageBatch,
        session: Session,
        tracker: ToolCallTracker,
        addr: MessageAddress,
        state: AddressState,
    ) -> bool:
        """Apply one inbound batch to the session and return whether the
        address loop should run an LLM turn after this batch."""
        needs_llm = False

        nudge_citations = False
        for result in batch.tool_results:
            tracker.handle_result(result, session)
            for entry in state.tool_call_trace:
                if entry.id == result.tool_call_id:
                    entry.result = stringify_tool_result(result.result)
                    break
            if any(result.tool_name.startswith(p) for p in CITATION_TOOL_PREFIXES):
                nudge_citations = True
        if batch.tool_results and not tracker.pending:
            self._flush_pending_system_events(session, state)
            # One reminder per batch is enough: dropping it once right before
            # the LLM call is what makes the rule "current" in context.
            if nudge_citations:
                session.append(SystemEvent(content=CITATION_REMINDER))
            if self.response.prior_turn_was_terminal(session):
                # The prior assistant turn used only terminal_when_lone
                # tools (e.g. send_media), which deliver the user-visible
                # reply directly. Calling the LLM again invites it to
                # write a chatty echo on top — skip the follow-up turn.
                logger.info(f"Skipping post-terminal LLM call for {addr}")
            else:
                needs_llm = True

        for event in batch.system_events:
            if tracker.pending:
                logger.debug(f"SystemEvent buffered (tools in flight): {event.content[:60]}")
                state.pending_system_events.append(event.content)
            else:
                session.append(SystemEvent(content=event.content))
                needs_llm = True

        for control in batch.control_events:
            self._apply_control_event(control, session, state, addr)

        if batch.user_messages:
            await self.bus.publish_outbound(TypingEvent(addr, is_typing=True))
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
            self._reset_turn_state(state)
            state.pending_media = list(user_event.media)
            needs_llm = True

        return needs_llm

    async def _process_llm_turn(
        self,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
        state: AddressState,
    ) -> TurnOutcome:
        build = self._build_prompt(session, addr, state.pending_media)
        if await self.compactor.maybe_compact(session, addr, build.messages):
            build = self._build_prompt(session, addr, state.pending_media)
        dump_messages(self.debug_dump_dir, addr, build.messages)
        state.pending_media.clear()
        response = await self._call_provider(addr, build.messages)
        if response is None:
            return TurnOutcome.DONE
        return await self.response.apply(response, session, tracker, call_ctx, addr, state)

    async def _address_loop(self, addr: MessageAddress) -> None:
        session = self.sessions.get(addr)
        tracker = ToolCallTracker()
        storage_layout.ensure_user_dirs(self.workspace_path, addr)
        call_ctx = ToolContext(
            workspace=self.workspace_path,
            bus=self.bus,
            media_repo=self.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
            storage_root=storage_layout.storage_root(self.workspace_path, addr).resolve(),
            read_roots=(
                storage_layout.skills_dir(self.workspace_path).resolve(),
                storage_layout.common_dir(self.workspace_path).resolve(),
            ),
            write_roots=(),
        )
        state = AddressState()
        # When the previous turn queued a follow-up (e.g. citation pushback),
        # keep the typing bubble on through the next consume so it doesn't
        # drop between the rejected reply and the follow-up call.
        last_outcome = TurnOutcome.DONE

        while True:
            if not tracker.pending and last_outcome is TurnOutcome.DONE:
                await self.bus.publish_outbound(TypingEvent(addr, is_typing=False))

            batch = await self.bus.consume_inbound_batch(address=addr)
            last_outcome = TurnOutcome.DONE
            needs_llm = await self._apply_batch(batch, session, tracker, addr, state)
            if not needs_llm:
                continue

            if state.iteration_count >= self.config.max_tool_iterations:
                logger.warning(f"Max tool iterations reached for {addr}")
                continue
            state.iteration_count += 1

            last_outcome = await self._process_llm_turn(session, tracker, call_ctx, addr, state)

    async def run(self) -> None:
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(self.sessions)
            await stack.enter_async_context(self.tools)
            logger.info("Agent loop started")
            new_addr_queue = self.bus.subscribe_new_addresses()

            try:
                async with asyncio.TaskGroup() as tg:

                    async def _dispatch() -> None:
                        while True:
                            addr = await new_addr_queue.get()
                            tg.create_task(self._address_loop(addr), name=f"agent-{addr}")

                    tg.create_task(_dispatch(), name="agent-dispatch")
            except* asyncio.CancelledError:
                # Cooperative shutdown: TaskGroup has already cancelled and
                # awaited every per-address task plus the dispatcher; swallow
                # the re-raised group so callers see a clean exit.
                pass
