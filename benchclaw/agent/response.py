"""LLM response handling: tool dispatch, citation validation/retry, outbound publish.

The agent loop hands every :class:`LLMResponse` to :meth:`ResponseHandler.apply`,
which classifies it (tool calls / empty / text), updates the session, dispatches
background tools, validates citations against the kb-tool history, and publishes
the user-visible message. Returns a :class:`TurnOutcome` that tells the loop
whether a follow-up LLM call is queued (citation pushback) or the turn is done.
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from benchclaw import citations as cit
from benchclaw.agent.loop_state import AddressState, ToolCallTracker, TurnOutcome
from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.registry import ToolRegistry
from benchclaw.bus import (
    MessageAddress,
    MessageBus,
    OutboundMessage,
    SystemMessageEvent,
    ToolCallTrace,
    ToolResultEvent,
)
from benchclaw.config import AgentConfig
from benchclaw.providers.base import LLMResponse, ToolCallRequest
from benchclaw.session import AssistantEvent, Session

# When any of these MCP tool prefixes returns a result, the agent loop drops
# a one-shot system reminder right after the ToolEvent so the citation rule
# is the most recent thing the model sees before composing its reply. Small
# models (Gemma-class) won't reliably follow a rule that lives only in the
# system prompt.
CITATION_TOOL_PREFIXES: tuple[str, ...] = ("kb__",)
CITATION_REMINDER = (
    "You just received results from a knowledge-base tool. Every claim in "
    "your next reply that draws on these results MUST be wrapped as "
    '<citation id="ID_FROM_RECORD">claim sentence</citation>, copying the '
    "`id` field verbatim from each record. Untagged paraphrase of kb "
    "content is not acceptable. The closing </citation> is required."
)

CITATION_MAX_RETRIES = 1


def stringify_tool_result(result: object) -> str:
    """Coerce a tool result to a string for the trace.

    We deliberately do NOT truncate or reflow here — the channel needs the
    raw text to extract structured payloads (e.g. kb_records for the
    citation listing). Display-time truncation belongs in the channel.
    """
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class ResponseHandler:
    """Applies one LLM response to the session and the bus."""

    def __init__(
        self,
        bus: MessageBus,
        tools: ToolRegistry,
        agent_config: AgentConfig,
        max_citation_retries: int = CITATION_MAX_RETRIES,
    ) -> None:
        self.bus = bus
        self.tools = tools
        self.config = agent_config
        self.max_citation_retries = max_citation_retries

    def prior_turn_was_terminal(self, session: Session) -> bool:
        """Did the most recent assistant turn consist only of
        terminal-when-lone tool calls?

        Used to suppress the empty-response nudge (and the post-tool LLM
        follow-up) when the model has already delivered the user-facing
        reply via a tool such as ``send_media``.
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

    async def apply(
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
            await self._dispatch_tool_calls(
                response, content, session, tracker, call_ctx, addr, state
            )
            return TurnOutcome.DONE

        if not content:
            return await self._handle_empty(session, addr)

        bad_ids, bad_refs, kb_records = cit.validate_citations(content, session.events)
        if bad_ids:
            if state.citation_retries < self.max_citation_retries:
                return await self._queue_citation_retry(
                    content, bad_ids, kb_records, session, addr, state
                )
            logger.warning(
                f"Invalid citations from {addr} after {self.max_citation_retries} "
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

    async def _dispatch_tool_calls(
        self,
        response: LLMResponse,
        content: str,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
        state: AddressState,
    ) -> None:
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

    async def _handle_empty(self, session: Session, addr: MessageAddress) -> TurnOutcome:
        if self.prior_turn_was_terminal(session):
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

    async def _queue_citation_retry(
        self,
        content: str,
        bad_ids: list[str],
        kb_records: dict[str, dict],
        session: Session,
        addr: MessageAddress,
        state: AddressState,
    ) -> TurnOutcome:
        state.citation_retries += 1
        # Keep the bad reply in history so the model sees what it produced;
        # the system reminder critiques it directly.
        session.append(AssistantEvent(content=content))
        valid_str = (
            ", ".join(sorted(kb_records))
            if kb_records
            else "(none — call a kb tool first if you need to cite)"
        )
        logger.warning(
            f"Invalid citations from {addr}: {', '.join(bad_ids)} "
            f"(retry {state.citation_retries}/{self.max_citation_retries})"
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
