"""Per-address agent loop state and the in-flight tool tracker.

Pulled out of ``loop.py`` so the orchestration in :class:`AgentLoop`
stays focused on the runtime; everything in here is data + a small
state machine for tracking background tool execution.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from benchclaw.bus import ToolCallTrace, ToolResultEvent
from benchclaw.session import Session, SystemEvent, ToolEvent


@dataclass
class AddressState:
    iteration_count: int = 0
    pending_system_events: list[str] = field(default_factory=list)
    pending_media: list[str] = field(default_factory=list)
    # Tool calls dispatched since the most recent user message, in order.
    # Reset whenever a new user message arrives.
    tool_call_trace: list[ToolCallTrace] = field(default_factory=list)
    # How many times we've already pushed back on the model in this turn
    # for citing a kb id that wasn't in the trace. Capped to keep one bad
    # reply from looping forever.
    citation_retries: int = 0
    # Set when we've just queued an inbound SystemMessageEvent that will
    # trigger another LLM call (e.g. a citation pushback). The address
    # loop honors this by skipping its top-of-loop typing=False publish
    # so the bubble doesn't drop between the rejected reply and the
    # follow-up call.
    expecting_followup_turn: bool = False


@dataclass(frozen=True)
class BatchApplication:
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
                content=(
                    f"Background tool '{event.tool_name}' completed. Summarize the "
                    "result for the user or take any necessary follow-up actions to "
                    "achieve the goal."
                )
            )
        )
        return True
