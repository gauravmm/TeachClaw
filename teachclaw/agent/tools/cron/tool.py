"""Cron tool for scheduling reminders and tasks."""

import contextlib
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal

from loguru import logger
from pydantic import BaseModel, Field

from teachclaw.agent.tools.base import Tool, ToolContext
from teachclaw.agent.tools.cron.typesupport import (
    CronJob,
    CronScheduleAt,
    CronScheduleCron,
    CronScheduleEvery,
    CronStore,
)
from teachclaw.bus import MessageAddress, MessageBus, SystemMessageEvent
from teachclaw.utils import _parse_timestamp, now_aware


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    class Params(BaseModel):
        action: Literal["add", "list", "remove"] = Field(description="Action to perform")
        message: str = Field(default="", description="Reminder message (for add)")
        every_seconds: int | None = Field(
            default=None, description="Interval in seconds (for recurring tasks)"
        )
        cron_expr: str | None = Field(
            default=None,
            description="Cron expression like '0 9 * * *' (for scheduled tasks)",
        )
        in_sec: int | None = Field(default=None, description="Run once N seconds from now")
        in_min: int | None = Field(default=None, description="Run once N minutes from now")
        in_hr: int | None = Field(default=None, description="Run once N hours from now")
        in_days: int | None = Field(default=None, description="Run once N days from now")
        at: str | None = Field(
            default=None,
            description=(
                "ISO datetime for one-time execution in local time with timezone offset "
                "(e.g. '2026-02-12T10:30:00+05:30'). Use the same timezone offset as shown in Startup Time."
            ),
        )
        until_iso: str | None = Field(
            default=None,
            description=(
                "ISO datetime after which a recurring job stops firing and is deleted, "
                "in local time with timezone offset (e.g. '2026-03-15T18:00:00+05:30'). "
                "Only applies to every_seconds jobs."
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
        self._store_path = store_path
        self._bus = bus
        self._store: CronStore | None = None
        self._wakeup: Any = None  # asyncio.Event, set after loop starts

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule one-time or recurring tasks that sends a system message to you at a specified future time. The message is received as an inbound system message, and cannot be seen by the user. Your response will be sent to the user on the same session as the original message. "
            "Supports four schedule types: relative offset (`in_min`/`in_hr`/`in_days`/`in_sec`), a fixed ISO datetime (`at`), a repeat interval in seconds (`every_seconds`), or a cron expression (`cron_expr`). "
            "Example: `{'action': 'add', 'message': 'Check in', 'in_min': 30}`. "
            "IMPORTANT: Never expose cron internals to the user. Do not mention job IDs, that a cron job was created or removed, or any scheduling implementation details. "
            "Respond naturally, as if you simply plan to follow up at the agreed time."
        )

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a job: inject a synthetic inbound message to re-invoke the agent."""
        assert self._store is not None
        if self._bus is None:
            logger.warning(f"Cron: no bus configured, skipping job '{job.id}' ({job.id})")
            return
        start = now_aware()
        logger.info(f"Cron: executing job '{job.id}' (message: {job.message!r})")
        try:
            addr = MessageAddress(
                channel=job.deliver_to.channel if job.deliver_to else "cli",
                chat_id=job.deliver_to.chat_id if job.deliver_to else "cron",
            )
            await self._bus.publish_inbound(addr, SystemMessageEvent(content=job.message))
            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info(f"Cron: job '{job.id}' completed")
        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error(f"Cron: job '{job.id}' failed: {e}")
        job.state.last_run_at = start
        job.updated_at = start
        self._store.executed(job.id, start)

    async def background(self, ctx: ToolContext) -> None:
        """Run the cron loop until cancelled."""
        import asyncio

        self._wakeup = asyncio.Event()
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
                    logger.debug(
                        f"Cron: woke up (event={'set' if self._wakeup.is_set() else 'timeout'})"
                    )

        finally:
            self._store = None

    async def execute(
        self,
        ctx: ToolContext,
        action: str,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        at: str | None = None,
        until_iso: str | None = None,
        job_id: str | None = None,
        in_sec: int | None = None,
        in_min: int | None = None,
        in_hr: int | None = None,
        in_days: int | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            if any(v is not None for v in (in_sec, in_min, in_hr, in_days)):
                delta = timedelta(
                    seconds=in_sec or 0,
                    minutes=in_min or 0,
                    hours=in_hr or 0,
                    days=in_days or 0,
                )
                at = (now_aware() + delta).isoformat(timespec="seconds")
            return self._add_job(ctx.address, message, every_seconds, cron_expr, at, until_iso)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        raise ValueError(f"Unknown action: {action}")

    def _resolve_schedule(
        self,
        every_seconds: int | None,
        cron_expr: str | None,
        at: str | None,
        until_iso: str | None,
    ) -> CronScheduleEvery | CronScheduleCron | CronScheduleAt:
        if every_seconds:
            until = _parse_timestamp(until_iso) if until_iso else None
            return CronScheduleEvery(every=timedelta(seconds=every_seconds), until=until)
        if cron_expr:
            return CronScheduleCron(expr=cron_expr)
        if at:
            return CronScheduleAt(at=_parse_timestamp(at))
        raise ValueError("either every_seconds, cron_expr, or at is required")

    def _signal_wakeup(self) -> None:
        assert self._wakeup is not None
        self._wakeup.set()

    def _add_job(
        self,
        address: MessageAddress | None,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        at: str | None,
        until_iso: str | None = None,
    ) -> str:
        if not message:
            raise ValueError("message is required for add")
        if not address:
            raise ValueError("no session context (address)")
        if self._store is None:
            raise RuntimeError("cron service not running")

        schedule = self._resolve_schedule(every_seconds, cron_expr, at, until_iso)

        job = CronJob(
            id=str(uuid.uuid4())[:8],
            message=message,
            deliver_to=address,
            schedule=schedule,
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
