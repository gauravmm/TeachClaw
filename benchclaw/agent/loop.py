"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from loguru import logger

from benchclaw import citations as cit
from benchclaw import personalities
from benchclaw import storage as storage_layout
from benchclaw.agent.cache_monitor import PromptCacheMonitor
from benchclaw.agent.dump import dump_messages
from benchclaw.agent.loop_state import (
    AddressState,
    BatchApplication,
    ToolCallTracker,
    TurnOutcome,
)
from benchclaw.agent.prompt import PromptBuild, PromptBuilder
from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.mcp_manager import MCPManager
from benchclaw.agent.tools.registry import ToolRegistry
from benchclaw.bus import (
    InboundMessage,
    InboundMessageBatch,
    MessageAddress,
    MessageBus,
    OutboundMessage,
    SessionControlEvent,
    SystemMessageEvent,
    ToolCallTrace,
    ToolResultEvent,
    TypingEvent,
)
from benchclaw.config import Config
from benchclaw.media import MediaRepository
from benchclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from benchclaw.session import (
    AssistantEvent,
    ConversationEvent,
    Session,
    SessionManager,
    SystemEvent,
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

# When any of these MCP tool prefixes returns a result, the agent loop drops
# a one-shot system reminder right after the ToolEvent so the citation rule
# is the most recent thing the model sees before composing its reply. Small
# models (Gemma-class) won't reliably follow a rule that lives only in the
# system prompt.
_CITATION_TOOL_PREFIXES: tuple[str, ...] = ("kb__",)
_CITATION_REMINDER = (
    "You just received results from a knowledge-base tool. Every claim in "
    "your next reply that draws on these results MUST be wrapped as "
    '<citation id="ID_FROM_RECORD">claim sentence</citation>, copying the '
    "`id` field verbatim from each record. Untagged paraphrase of kb "
    "content is not acceptable. The closing </citation> is required."
)


_CITATION_MAX_RETRIES = 1


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
        self.cache_monitor = PromptCacheMonitor()
        self.prompt_builder = PromptBuilder(
            config.workspace_path,
            tools=self.tools,
            media_repo=media_repo,
            agent_config=self.config,
        )

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
            options=self.prompt_builder.render_options(),
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
                top_p=self.config.top_p,
                top_k=self.config.top_k,
                enable_thinking=False if self.config.enable_thinking else None,
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

    @staticmethod
    def _stringify_tool_result(result: object) -> str:
        """Coerce a tool result to a string for the trace.

        We deliberately do NOT truncate or reflow here — the channel needs
        the raw text to extract structured payloads (e.g. kb_records for
        the citation listing). Display-time truncation belongs in the
        channel.
        """
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    async def _apply_llm_response(
        self,
        response: LLMResponse,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
        state: AddressState,
    ) -> TurnOutcome:
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
                state.tool_call_trace.append(
                    ToolCallTrace(id=tc.id, name=tc.name, arguments=dict(tc.arguments))
                )
                task = asyncio.create_task(
                    self._run_tool_and_post(tc, call_ctx, addr),
                    name=f"tool-{tc.id[:8]}",
                )
                tracker.add(tc.id, tc.name, task)
            if content:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        address=addr,
                        content=content,
                        metadata={"tool_calls": list(state.tool_call_trace)},
                    )
                )
            return TurnOutcome.DONE

        if not content:
            if self._prior_turn_was_terminal(session):
                logger.info(
                    f"LLM returned empty response for {addr} after a terminal tool call — no nudge"
                )
                return TurnOutcome.DONE
            logger.warning(
                f"LLM returned empty response (no text, no tool calls) for {addr} — injecting nudge"
            )
            await self.bus.publish_inbound(
                addr,
                SystemMessageEvent(
                    content="You did not provide a text response. Please respond to the user now."
                ),
            )
            return TurnOutcome.DONE
        bad_ids, bad_refs, kb_records = cit.validate_citations(content, session.events)
        if bad_ids:
            if state.citation_retries < _CITATION_MAX_RETRIES:
                state.citation_retries += 1
                # Keep the bad reply in history so the model sees what it
                # produced; the system reminder critiques it directly.
                session.append(AssistantEvent(content=content))
                valid_str = (
                    ", ".join(sorted(kb_records))
                    if kb_records
                    else "(none — call a kb tool first if you need to cite)"
                )
                logger.warning(
                    f"Invalid citations from {addr}: {', '.join(bad_ids)} "
                    f"(retry {state.citation_retries}/{_CITATION_MAX_RETRIES})"
                )
                await self.bus.publish_inbound(
                    addr,
                    SystemMessageEvent(
                        content=(
                            f"Citation ids {', '.join(bad_ids)} are not in any kb "
                            f"result in this session. Valid ids: {valid_str}. "
                            "Rewrite the reply using only valid ids, or drop the "
                            "unsupported claims."
                        )
                    ),
                )
                return TurnOutcome.RETRY_QUEUED
            logger.warning(
                f"Invalid citations from {addr} after {_CITATION_MAX_RETRIES} "
                f"retries: {', '.join(bad_ids)}; publishing with verifiability "
                "postscript."
            )
            content = cit.unverified_postscript(content, bad_refs)

        session.append(AssistantEvent(content=content))
        preview = content[:120] + "..." if len(content) > 120 else content
        logger.info(f"Response to {addr}: {preview}")
        await self.bus.publish_outbound(
            OutboundMessage(
                address=addr,
                content=content,
                metadata={"tool_calls": list(state.tool_call_trace)},
            )
        )
        return TurnOutcome.DONE

    def _prior_turn_was_terminal(self, session: Session) -> bool:
        """Did the most recent assistant turn consist only of terminal-when-lone tool calls?

        Used to suppress the empty-response nudge when the model has already
        delivered the user-facing reply via a tool (e.g. ``send_media``).
        """
        for event in reversed(session.events):
            if not isinstance(event, AssistantEvent):
                continue
            if not event.tool_calls:
                return False
            for tc in event.tool_calls:
                name = tc.get("function", {}).get("name", "")
                if not self.tools.is_terminal_when_lone(name):
                    return False
            return True
        return False

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

    def _apply_batch(
        self,
        batch: InboundMessageBatch,
        session: Session,
        tracker: ToolCallTracker,
        addr: MessageAddress,
        state: AddressState,
    ) -> BatchApplication:
        needs_llm = False
        start_typing = False

        nudge_citations = False
        for result in batch.tool_results:
            tracker.handle_result(result, session)
            for entry in state.tool_call_trace:
                if entry.id == result.tool_call_id:
                    entry.result = self._stringify_tool_result(result.result)
                    break
            if any(result.tool_name.startswith(p) for p in _CITATION_TOOL_PREFIXES):
                nudge_citations = True
        if batch.tool_results and not tracker.pending:
            self._flush_pending_system_events(session, state)
            # One reminder per batch is enough: dropping it once right before
            # the LLM call is what makes the rule "current" in context.
            if nudge_citations:
                session.append(SystemEvent(content=_CITATION_REMINDER))
            if self._prior_turn_was_terminal(session):
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
            self._reset_turn_state(state)
            state.pending_media = list(user_event.media)
            needs_llm = True

        return BatchApplication(needs_llm=needs_llm, start_typing=start_typing)

    async def _process_llm_turn(
        self,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
        state: AddressState,
    ) -> TurnOutcome:
        build = self._build_prompt(session, addr, state.pending_media)
        if await self._maybe_compact_proactive(session, addr, build.messages):
            build = self._build_prompt(session, addr, state.pending_media)
        dump_messages(self.debug_dump_dir, addr, build.messages)
        state.pending_media.clear()
        response = await self._call_provider(addr, build.messages)
        if response is None:
            return TurnOutcome.DONE
        return await self._apply_llm_response(response, session, tracker, call_ctx, addr, state)

    async def _address_loop(self, addr: MessageAddress) -> None:
        session = self.sessions.get(addr)
        tracker = ToolCallTracker()
        storage_layout.ensure_user_dirs(self.workspace_path, addr)
        storage_root = storage_layout.storage_root(self.workspace_path, addr).resolve()
        read_roots: tuple[Path, ...] = (
            storage_layout.skills_dir(self.workspace_path).resolve(),
            storage_layout.common_dir(self.workspace_path).resolve(),
        )
        write_roots: tuple[Path, ...] = ()
        call_ctx = ToolContext(
            workspace=self.workspace_path,
            bus=self.bus,
            media_repo=self.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
            storage_root=storage_root,
            read_roots=read_roots,
            write_roots=write_roots,
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
            batch_result = self._apply_batch(batch, session, tracker, addr, state)
            if batch_result.start_typing:
                await self.bus.publish_outbound(TypingEvent(addr, is_typing=True))
            if not batch_result.needs_llm:
                continue

            if state.iteration_count >= self.config.max_tool_iterations:
                logger.warning(f"Max tool iterations reached for {addr}")
                continue
            state.iteration_count += 1

            last_outcome = await self._process_llm_turn(session, tracker, call_ctx, addr, state)

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
