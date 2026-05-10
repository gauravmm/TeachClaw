"""Per-address agent loop state and the in-flight tool tracker.

Pulled out of ``loop.py`` so the orchestration in :class:`AgentLoop`
stays focused on the runtime; everything in here is data + a small
state machine for tracking background tool execution.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from teachclaw.bus import SystemMessageEvent, ToolCallTrace, ToolResultEvent
from teachclaw.session import Session, SystemEvent, ToolEvent

if TYPE_CHECKING:
    from teachclaw.config import ToolReminder


class TurnOutcome(Enum):
    """Result of one LLM turn, signaling how the address loop should bridge
    into the next iteration."""

    DONE = "done"
    # The turn ended by queuing an inbound SystemMessageEvent that will
    # trigger a follow-up LLM call (e.g. a citation pushback). The address
    # loop should keep the typing bubble on so it doesn't drop between the
    # rejected reply and the follow-up call.
    RETRY_QUEUED = "retry_queued"


@dataclass
class AddressState:
    iteration_count: int = 0
    pending_system_events: list[SystemMessageEvent] = field(default_factory=list)
    pending_media: list[str] = field(default_factory=list)
    # Tool calls dispatched since the most recent user message, in order.
    # Reset whenever a new user message arrives.
    tool_call_trace: list[ToolCallTrace] = field(default_factory=list)
    # How many times we've already pushed back on the model in this turn
    # for citing a kb id that wasn't in the trace. Capped to keep one bad
    # reply from looping forever.
    citation_retries: int = 0


class ToolCallTracker:
    """Per-address tracker for in-flight background tool calls."""

    def __init__(self, reminders: dict[str, ToolReminder] | None = None) -> None:
        self._in_flight: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._reminders: dict[str, ToolReminder] = reminders or {}

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

    def handle_result(self, event: ToolResultEvent, session: Session) -> None:
        self._tasks.pop(event.tool_call_id, None)
        session.append(
            ToolEvent(
                content=event.result,
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_name,
            )
        )
        if reminder := self._reminders.get(event.tool_name):
            session.append(SystemEvent(content=reminder.text, ephemeral=reminder.ephemeral))
        if event.tool_call_id in self._in_flight:
            del self._in_flight[event.tool_call_id]
            return

        session.append(
            SystemEvent(
                content=(
                    f"Background tool '{event.tool_name}' completed. Summarize the "
                    "result for the user or take any necessary follow-up actions to "
                    "achieve the goal."
                )
            )
        )
