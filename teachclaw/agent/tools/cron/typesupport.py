"""Cron types."""

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Literal

from heapdict import heapdict
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from teachclaw.utils import (
    DurationField,
    MessageAddressField,
    OptionalTimestampSerializer,
    TimestampSerializer,
    format_duration,
    now_aware,
)


class CronModel(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)


class CronScheduleAt(CronModel):
    """Run once at a specific datetime."""

    at: OptionalTimestampSerializer = None

    def next_run(self, dt: datetime) -> datetime | None:
        return self.at if self.at and self.at > dt else None

    def __str__(self):
        return f"at {self.at.isoformat(timespec='seconds') if self.at else 'never'}"


class CronScheduleEvery(CronModel):
    """Run repeatedly with a fixed interval, anchored to a starting time."""

    every: DurationField = Field(default_factory=lambda: timedelta(hours=1))
    anchor: TimestampSerializer = Field(default_factory=now_aware)
    until: OptionalTimestampSerializer = None

    def next_run(self, dt: datetime) -> datetime | None:
        if self.every <= timedelta(0):
            return None
        elapsed_s = (dt - self.anchor).total_seconds()
        n = math.floor(elapsed_s / self.every.total_seconds()) + 1
        t = self.anchor + n * self.every
        if self.until is not None and t > self.until:
            return None
        return t

    def __str__(self) -> str:
        base = f"every {format_duration(self.every)}"
        if self.until is not None:
            return f"{base} until {self.until.strftime('%Y-%m-%d %H:%M')}"
        return base


class CronScheduleCron(CronModel):
    """Run on a cron expression schedule."""

    expr: str = ""  # required; empty string is invalid
    tz: str = ""  # IANA timezone name; empty = local timezone

    def next_run(self, dt: datetime) -> datetime | None:
        if not self.expr:
            return None
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter

            start = dt.astimezone(ZoneInfo(self.tz)) if self.tz else dt.astimezone()
            cron = croniter(self.expr, start)
            return cron.get_next(datetime).astimezone()
        except Exception:
            return None

    def __str__(self) -> str:
        return f"cron '{self.expr}'" + (f" ({self.tz})" if self.tz else "")


CronSchedule = CronScheduleAt | CronScheduleEvery | CronScheduleCron


class CronJobState(CronModel):
    """Runtime state of a job."""

    last_run_at: OptionalTimestampSerializer = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


class CronJob(CronModel):
    """A scheduled job."""

    id: str
    message: str
    deliver_to: MessageAddressField = None
    state: CronJobState = Field(default_factory=CronJobState)
    enabled: bool = True
    schedule: CronSchedule = Field(default_factory=CronScheduleEvery)
    created_at: TimestampSerializer = Field(default_factory=now_aware)
    updated_at: TimestampSerializer = Field(default_factory=now_aware)

    @field_validator("schedule", mode="before")
    @classmethod
    def _validate_schedule(
        cls,
        value: CronSchedule | dict,
    ) -> CronScheduleAt | CronScheduleEvery | CronScheduleCron:
        if isinstance(value, (CronScheduleAt, CronScheduleEvery, CronScheduleCron)):
            return value
        if "at" in value:
            return CronScheduleAt.model_validate(value)
        if "every" in value:
            return CronScheduleEvery.model_validate(value)
        if "expr" in value:
            return CronScheduleCron.model_validate(value)
        raise ValueError(f"Unknown schedule kind: {', '.join(value.keys())}")


class CronData(CronModel):
    """Persistent store for cron jobs."""

    version: int = 1
    jobs: list[CronJob] = Field(default_factory=list)


class CronStore:
    """Async context manager: loads on enter, always writes back on exit."""

    def __init__(self, path: Path):
        self._path = path
        self._store: dict[str, CronJob] = {}
        self._queue: heapdict = heapdict()  # jid -> next_run_at

    async def __aenter__(self) -> "CronStore":
        try:
            data = CronData.model_validate_json(self._path.read_text())
            now = now_aware()
            for j in data.jobs:
                next_run = j.schedule.next_run(now)
                if next_run is None:
                    continue  # expired; skip so it's dropped on next write
                self._store[j.id] = j
                if j.enabled:
                    self._queue[j.id] = next_run
        except IOError as e:
            logger.warning(f"No cron store at {e}. Creating one from scratch.")
        return self

    async def __aexit__(self, *_) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(CronData(jobs=list(self._store.values())).model_dump_json(indent=2))

    def jobs(self) -> Iterable[CronJob]:
        return self._store.values()

    def get(self, jid: str) -> CronJob | None:
        return self._store.get(jid)

    def add(self, j: CronJob) -> None:
        """Add or replace a job and update the queue."""
        self._store[j.id] = j
        if j.enabled:
            next_run = j.schedule.next_run(now_aware())
            if next_run is not None:
                self._queue[j.id] = next_run
                return
        if j.id in self._queue:
            del self._queue[j.id]

    def remove(self, jid: str) -> bool:
        """Remove a job by ID. Returns True if it existed."""
        if jid not in self._store:
            return False
        del self._store[jid]
        if jid in self._queue:
            del self._queue[jid]

        return True

    def enable(self, jid: str, enabled: bool, now: datetime) -> bool:
        """Enable or disable a job. Returns False if job not found."""
        if (job := self._store.get(jid)) is None:
            return False
        job.enabled = enabled
        job.updated_at = now
        if enabled:
            next_run = job.schedule.next_run(now)
            if next_run is not None:
                self._queue[jid] = next_run
            elif jid in self._queue:
                del self._queue[jid]
        else:
            if jid in self._queue:
                del self._queue[jid]

        return True

    def executed(self, jid: str, now: datetime) -> None:
        """Post-execution: remove one-shot or expired jobs, reschedule recurring ones."""
        if (job := self._store.get(jid)) is None:
            return
        if isinstance(job.schedule, CronScheduleAt):
            self.remove(jid)
            return
        next_run = job.schedule.next_run(now)
        if next_run is not None:
            self._queue[jid] = next_run
        else:
            self.remove(jid)  # auto-delete expired recurring jobs

    def next_run_for(self, jid: str) -> datetime | None:
        """Return the queued next run time for a specific job, or None."""
        return self._queue.get(jid)

    def next_wake(self) -> datetime | None:
        """Return the earliest scheduled next_run_at, or None."""
        if not self._queue:
            return None
        _, dt = self._queue.peekitem()
        return dt

    def pop_due(self, now: datetime) -> list[CronJob]:
        """Remove and return all jobs due at or before now."""
        due = []
        while self._queue:
            jid, next_run = self._queue.peekitem()
            if next_run > now:
                break
            self._queue.popitem()
            job = self._store.get(jid)
            if job is None:
                logger.warning(
                    f"Cron: job '{jid}' was in queue but not in store (ghost entry); skipping"
                )
                continue
            if not job.enabled:
                logger.debug(f"Cron: job '{jid}' was due but is disabled; skipping")
                continue
            due.append(job)
        return due
