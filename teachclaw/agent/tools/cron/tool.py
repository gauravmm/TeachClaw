"""Cron tool for scheduling reminders and tasks."""

import contextlib
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal

from loguru import logger
from pydantic import BaseModel, Field

from teachclaw.agent.tools.base import Tool, ToolContext
from teachclaw.agent.tools.cron.typesupport import (
    CronJob,
    CronSchedule,
    CronScheduleAt,
    CronScheduleEvery,
    CronStore,
)
from teachclaw.bus import MessageAddress, MessageBus, SystemMessageEvent
from teachclaw.utils import _parse_timestamp, now_aware, parse_duration


def _parse_when(value: str) -> datetime:
    """Resolve a ``delay`` argument to an absolute datetime.

    Accepts either an ISO 8601 timestamp (``2026-02-12T10:30:00+05:30``)
    or a duration string relative to now (``30s``, ``2m``, ``3d``,
    ``1h30m``). The duration form is the agent's natural register; the
    ISO form is the escape hatch for "fire at this exact moment".
    """
    text = value.strip()
    # ISO timestamps always carry a `-` (date) or `T` (date/time
    # separator). Durations like ``30s`` / ``1h30m`` carry neither.
    if "-" in text or "T" in text:
        try:
            return _parse_timestamp(text)
        except ValueError:
            pass
    return now_aware() + parse_duration(text, positive=False)


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    class Params(BaseModel):
        action: Literal["add", "list", "remove"] = Field(description="Action to perform")
        message: str = Field(default="", description="Reminder message (for add)")
        delay: str | None = Field(
            default=None,
            description=(
                "When the first (or only) fire should happen — either a duration from "
                "now (``30s``, ``2m``, ``3d``, ``1h30m``) or an ISO 8601 timestamp "
                "with timezone offset (``2026-02-12T10:30:00+05:30``). Without "
                "``every``, this schedules a one-shot."
            ),
        )
        every: str | None = Field(
            default=None,
            description=(
                "Recurrence interval as a duration string (``5m``, ``1h``, ``1d``). "
                "First fire is at ``delay`` if provided, otherwise one full ``every`` "
                "from now."
            ),
        )
        until: str | None = Field(
            default=None,
            description=(
                "ISO 8601 timestamp after which a recurring job stops firing and is "
                "deleted (``2026-03-15T18:00:00+05:30``). Only meaningful with ``every``."
            ),
        )
        job_id: str | None = Field(default=None, description="Job ID (for remove)")

    Params: ClassVar[type[BaseModel]] = Params

    @classmethod
    def build(cls, config: None, ctx: ToolContext) -> "CronTool":
        return cls(
            store_path=ctx.workspace / "cron" / "jobs.json",
            bus=ctx.bus,
        )

    def __init__(
        self,
        store_path: Path,
        bus: MessageBus | None,
    ):
        import asyncio

        self._store_path = store_path
        self._bus = bus
        self._store: CronStore | None = None
        self._wakeup = asyncio.Event()

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule a future system message to yourself. The message arrives as an "
            "inbound system event (the user does not see it); your reply goes to the "
            "same chat the original message came from. "
            "Provide ``delay`` (one-shot) and/or ``every`` (recurring); use ``until`` "
            "to bound a recurring job. Both ``delay`` and ``every`` accept duration "
            "strings (``30s``, ``2m``, ``1h30m``); ``delay`` also accepts an ISO "
            "timestamp. Examples: "
            "``{'action': 'add', 'message': 'Check in', 'delay': '30m'}`` (fires once); "
            "``{'action': 'add', 'message': 'Hourly heartbeat', 'every': '1h'}``. "
            "IMPORTANT: never expose cron internals to the user — no job IDs, no "
            "mention of scheduling. Speak as if you simply plan to follow up."
        )

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a job: inject a synthetic inbound message to re-invoke the agent."""
        assert self._store is not None
        if self._bus is None:
            logger.warning(f"Cron: no bus configured, skipping job '{job.id}'")
            return
        start = now_aware()
        logger.info(f"Cron: executing job '{job.id}' (message: {job.message!r})")
        addr = MessageAddress(
            channel=job.deliver_to.channel if job.deliver_to else "cli",
            chat_id=job.deliver_to.chat_id if job.deliver_to else "cron",
        )
        try:
            await self._bus.publish_inbound(addr, SystemMessageEvent(content=job.message))
            logger.info(f"Cron: job '{job.id}' completed")
        except Exception as e:
            logger.error(f"Cron: job '{job.id}' failed: {e}")
        self._store.executed(job.id, start)

    async def run_loop(self) -> None:
        """Run the cron loop until cancelled. Started as a task by AgentLoop."""
        import asyncio

        try:
            async with CronStore(self._store_path) as store:
                self._store = store
                while True:
                    now = now_aware()
                    due = store.pop_due(now)
                    if due:
                        logger.debug(f"Cron: {len(due)} job(s) due: {[j.id for j in due]}")
                    for job in due:
                        await self._execute_job(job)

                    next_wake = store.next_wake()
                    delay = max(0.0, (next_wake - now).total_seconds()) if next_wake else 60.0
                    logger.debug(f"Cron: sleeping {delay:.1f}s (next_wake={next_wake})")
                    self._wakeup.clear()
                    with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                        await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
        finally:
            self._store = None

    async def execute(
        self,
        ctx: ToolContext,
        action: str,
        message: str = "",
        delay: str | None = None,
        every: str | None = None,
        until: str | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            return self._add_job(ctx.address, message, delay, every, until)
        if action == "list":
            return self._list_jobs()
        if action == "remove":
            return self._remove_job(job_id)
        raise ValueError(f"Unknown action: {action}")

    @staticmethod
    def _resolve_schedule(
        delay: str | None,
        every: str | None,
        until: str | None,
    ) -> CronSchedule:
        if every is None and delay is None:
            raise ValueError("either delay or every (or both) is required")
        every_td: timedelta | None = parse_duration(every) if every else None
        until_dt: datetime | None = _parse_timestamp(until) if until else None
        if every_td is None:
            assert delay is not None
            return CronScheduleAt(at=_parse_when(delay))
        anchor = _parse_when(delay) if delay else now_aware() + every_td
        return CronScheduleEvery(every=every_td, anchor=anchor, until=until_dt)

    def _signal_wakeup(self) -> None:
        self._wakeup.set()

    def _add_job(
        self,
        address: MessageAddress | None,
        message: str,
        delay: str | None,
        every: str | None,
        until: str | None,
    ) -> str:
        if not message:
            raise ValueError("message is required for add")
        if not address:
            raise ValueError("no session context (address)")
        if self._store is None:
            raise RuntimeError("cron service not running")

        job = CronJob(
            id=str(uuid.uuid4())[:8],
            message=message,
            deliver_to=address,
            schedule=self._resolve_schedule(delay, every, until),
        )
        self._store.add(job)
        self._signal_wakeup()
        return f"Created job '{job.id}'"

    def _list_jobs(self) -> str:
        if self._store is None:
            raise RuntimeError("cron service not running")
        jobs = self._store.jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = [f"- {j.id}: {j.schedule}" for j in jobs]
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            raise ValueError("job_id is required for remove")
        if self._store is None:
            raise RuntimeError("cron service not running")
        if self._store.remove(job_id):
            self._signal_wakeup()
            return f"Removed job {job_id}"
        raise KeyError(f"job {job_id} not found")
