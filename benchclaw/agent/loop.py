"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from benchclaw import personalities
from benchclaw import storage as storage_layout
from benchclaw.agent.cache_monitor import PromptCacheMonitor
from benchclaw.agent.context import build_system_prompt
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
    RenderOptions,
    Session,
    SessionManager,
    SystemEvent,
    ToolEvent,
    UserEvent,
)
from benchclaw.utils import now_aware

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


@dataclass
class _AddressState:
    iteration_count: int = 0
    pending_system_events: list[str] = field(default_factory=list)
    pending_media: list[str] = field(default_factory=list)
    # Tool calls dispatched since the most recent user message, in order.
    # Reset whenever a new user message arrives.
    tool_call_trace: list[ToolCallTrace] = field(default_factory=list)


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

        self.sessions = SessionManager(config.workspace_path / "sessions")

        master_ctx = ToolContext(
            workspace=config.workspace_path,
            bus=bus,
            media_repo=media_repo,
        )
        self.master_ctx = master_ctx
        mcp_manager = MCPManager(config.mcp_servers) if config.mcp_servers else None
        self.tools = ToolRegistry(config.tools, master_ctx, mcp_manager=mcp_manager)
        self.cache_monitor = PromptCacheMonitor()

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
                json.dumps(
                    [self._inflate_for_dump(m) for m in messages],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to write debug dump: {e}")

    @staticmethod
    def _try_parse_json_string(value: object) -> object:
        """Inflate a string that's actually JSON or newline-delimited JSON.

        Tool results and tool-call arguments live in OpenAI-shaped messages
        as opaque strings, which makes the debug dump unreadable: every
        quote and newline gets re-escaped on the outer ``json.dumps``. For
        the dump we walk those strings and substitute the parsed value
        when parsing succeeds; the wire messages stay untouched.
        """
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped or stripped[0] not in "{[":
            return value
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
        # JSONL fallback for tools that emit one record per line (kb__).
        lines = [ln for ln in stripped.splitlines() if ln.strip()]
        if len(lines) < 2:
            return value
        parsed: list[object] = []
        for ln in lines:
            try:
                parsed.append(json.loads(ln))
            except json.JSONDecodeError:
                return value
        return parsed

    @classmethod
    def _inflate_for_dump(cls, message: dict[str, object]) -> dict[str, object]:
        out = dict(message)
        if out.get("role") == "tool":
            out["content"] = cls._try_parse_json_string(out.get("content"))
        tool_calls = out.get("tool_calls")
        if isinstance(tool_calls, list):
            inflated_calls: list[object] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    inflated_calls.append(tc)
                    continue
                tc_copy = dict(tc)
                fn = tc_copy.get("function")
                if isinstance(fn, dict):
                    fn_copy = dict(fn)
                    fn_copy["arguments"] = cls._try_parse_json_string(fn_copy.get("arguments"))
                    tc_copy["function"] = fn_copy
                inflated_calls.append(tc_copy)
            out["tool_calls"] = inflated_calls
        return out

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

    def _render_options(self) -> RenderOptions:
        elide_tools: tuple[str, ...] = ()
        if self.config.compaction.elide_chunks_after_turn:
            elide_tools = tuple(self.config.compaction.elide_tool_names)
        return RenderOptions(elide_tool_names=elide_tools)

    def _build_prompt_and_messages(
        self,
        session: Session,
        addr: MessageAddress,
        pending_media: list[str] | None,
    ) -> list[dict[str, object]]:
        storage_root = storage_layout.storage_root(self.workspace_path, addr)
        persona = personalities.read_personality(self.workspace_path, addr)
        prompt = build_system_prompt(
            self.workspace_path,
            tools=self.tools.values(),
            channel=addr.channel,
            chat_id=addr.chat_id,
            session_label=session.describe_current_session(),
            chunk_elision_active=self.config.compaction.elide_chunks_after_turn,
            profile_text=storage_layout.read_profile(self.workspace_path, addr),
            storage_path=str(storage_root.expanduser().resolve()),
            model=self.config.model,
            context_window=self.config.context_window,
        )
        messages = session.render_llm_messages(prompt, self._render_options())
        listing = storage_layout.listing_for_user(self.workspace_path, addr)
        current_time = now_aware().strftime("%Y-%m-%d %H:%M (%A) %z")
        media_blocks = (
            self.media_repo.build_media_blocks(addr, pending_media)
            if (pending_media and self.media_repo)
            else None
        )
        out, stable_prefix_end = self._inject_tail(
            messages,
            listing=listing,
            current_time=current_time,
            persona_overlay=persona.overlay,
            media_blocks=media_blocks,
        )
        self.cache_monitor.observe(addr, out, stable_prefix_end)
        return out

    @staticmethod
    def _inject_tail(
        messages: list[dict[str, object]],
        *,
        listing: str | None,
        current_time: str | None,
        persona_overlay: str | None,
        media_blocks: list[dict[str, object]] | None,
    ) -> tuple[list[dict[str, object]], int]:
        """Attach turn-local context to the latest user turn.

        Returns ``(messages, stable_prefix_end)`` where ``stable_prefix_end``
        is the exclusive index up to which the prefix should be cache-stable
        across turns: everything before the synthetic injection (when one was
        added) or before the latest user message (when nothing was injected).

        The synthetic message carries ``<current_time>``,
        ``<storage_listing>``, and ``<persona>``; it goes in right before
        the most recent user turn so the cacheable system-prompt prefix
        stays stable. Media blocks are prepended to the latest user
        message's content, promoting plain text into a content-block list
        when needed.
        """
        last_user_idx = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
            None,
        )
        persona_text = (persona_overlay or "").strip() or None
        has_synthetic = bool(listing or current_time or persona_text)
        if last_user_idx is None or (not has_synthetic and not media_blocks):
            return list(messages), last_user_idx if last_user_idx is not None else len(messages)

        out = list(messages)
        if media_blocks:
            user_msg = dict(out[last_user_idx])
            existing = user_msg.get("content", "")
            if isinstance(existing, list):
                user_msg["content"] = [*media_blocks, *existing]
            else:
                user_msg["content"] = [*media_blocks, {"type": "text", "text": existing}]
            out[last_user_idx] = user_msg

        if not has_synthetic:
            return out, last_user_idx

        parts: list[str] = []
        if current_time:
            parts.append(f"<current_time>{current_time}</current_time>")
        if listing:
            parts.append(f"<storage_listing>\n{listing}\n</storage_listing>")
        if persona_text:
            parts.append(f"<persona>\n{persona_text}\n</persona>")
        ctx_msg: dict[str, object] = {
            "role": "user",
            "content": "\n".join(parts),
        }
        out.insert(last_user_idx, ctx_msg)
        # After insert, the synthetic message sits at last_user_idx and the
        # stable prefix is everything strictly before it.
        return out, last_user_idx

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
            options=self._render_options(),
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
        state: _AddressState,
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
            return

        if not content:
            if self._prior_turn_was_terminal(session):
                logger.info(
                    f"LLM returned empty response for {addr} after a terminal tool call — no nudge"
                )
                return
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
        await self.bus.publish_outbound(
            OutboundMessage(
                address=addr,
                content=content,
                metadata={"tool_calls": list(state.tool_call_trace)},
            )
        )

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
    def _flush_pending_system_events(session: Session, state: _AddressState) -> None:
        for content in state.pending_system_events:
            session.append(SystemEvent(content=content))
        state.pending_system_events.clear()

    def _apply_control_event(
        self,
        event: SessionControlEvent,
        session: Session,
        state: _AddressState,
        addr: MessageAddress,
    ) -> None:
        if event.action == "reset":
            session.clear()
            personalities.clear_personality(self.workspace_path, addr)
            self.cache_monitor.forget(addr)
            state.pending_system_events.clear()
            state.tool_call_trace = []
            state.iteration_count = 0
            logger.info(f"Session reset for {addr}")
        elif event.action == "forget":
            session.clear()
            self.cache_monitor.forget(addr)
            state.pending_system_events.clear()
            state.tool_call_trace = []
            state.iteration_count = 0
            root = storage_layout.storage_root(self.workspace_path, addr)
            if root.exists():
                import shutil

                try:
                    shutil.rmtree(root)
                    logger.info(f"Storage forgotten for {addr}: removed {root}")
                except OSError as e:
                    logger.error(f"Failed to remove storage for {addr}: {e}")
        else:
            logger.warning(f"Unknown SessionControlEvent action: {event.action!r}")

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
            state.pending_media = list(user_event.media)
            state.iteration_count = 0
            state.tool_call_trace = []
            needs_llm = True

        return _BatchApplication(needs_llm=needs_llm, start_typing=start_typing)

    async def _process_llm_turn(
        self,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
        state: _AddressState,
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
        await self._apply_llm_response(response, session, tracker, call_ctx, addr, state)

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
                state,
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
