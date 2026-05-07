"""Cron types — schedule shapes, the persistent ``CronJob`` model, and a
small dict-backed store.

The job count per workspace is expected to be small (single digits), so
the store doesn't bother with a priority queue: ``next_wake`` and
``pop_due`` just scan the dict each tick. ``schedule.next_run(ref)``
returns the next fire time strictly after ``ref``, or the first ever
fire when ``ref is None``."""

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

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

    at: TimestampSerializer

    def next_run(self, ref: datetime | None) -> datetime | None:
        """First fire time strictly after ``ref``.

        ``ref is None`` (never fired) returns the scheduled time, even
        if ``at`` is now in the past — past-due one-shots fire on the
        next tick rather than vanishing silently.
        """
        if ref is None:
            return self.at
        return self.at if self.at > ref else None

    def __str__(self) -> str:
        return f"at {self.at.isoformat(timespec='seconds')}"


class CronScheduleEvery(CronModel):
    """Run repeatedly. ``anchor`` is the first fire time; subsequent
    fires happen at ``anchor + N * every``. Optionally bounded by
    ``until``.
    """

    every: DurationField = Field(default_factory=lambda: timedelta(hours=1))
    anchor: TimestampSerializer = Field(default_factory=now_aware)
    until: OptionalTimestampSerializer = None

    def next_run(self, ref: datetime | None) -> datetime | None:
        """Strict-greater-than next run. ``ref is None`` (never fired) or
        ``ref < anchor`` returns the anchor itself — i.e. the first fire."""
        if self.every <= timedelta(0):
            return None
        if ref is None or ref < self.anchor:
            candidate = self.anchor
        else:
            elapsed = (ref - self.anchor).total_seconds()
            n = math.floor(elapsed / self.every.total_seconds()) + 1
            candidate = self.anchor + n * self.every
        if self.until is not None and candidate > self.until:
            return None
        return candidate

    def __str__(self) -> str:
        base = (
            f"every {format_duration(self.every)} starting {self.anchor.strftime('%Y-%m-%d %H:%M')}"
        )
        if self.until is not None:
            return f"{base} until {self.until.strftime('%Y-%m-%d %H:%M')}"
        return base


CronSchedule = CronScheduleAt | CronScheduleEvery


class CronJobState(CronModel):
    """Runtime state of a job. Updated by :meth:`CronStore.executed`."""

    last_run_at: OptionalTimestampSerializer = None


class CronJob(CronModel):
    """A scheduled job."""

    id: str
    message: str
    deliver_to: MessageAddressField = None
    state: CronJobState = Field(default_factory=CronJobState)
    schedule: CronSchedule = Field(default_factory=CronScheduleEvery)
    created_at: TimestampSerializer = Field(default_factory=now_aware)
    updated_at: TimestampSerializer = Field(default_factory=now_aware)

    @field_validator("schedule", mode="before")
    @classmethod
    def _validate_schedule(cls, value: CronSchedule | dict) -> CronSchedule:
        if isinstance(value, (CronScheduleAt, CronScheduleEvery)):
            return value
        if "at" in value:
            return CronScheduleAt.model_validate(value)
        if "every" in value:
            return CronScheduleEvery.model_validate(value)
        raise ValueError(f"Unknown schedule kind: {', '.join(value.keys())}")

    def next_run(self) -> datetime | None:
        """Convenience: schedule.next_run(state.last_run_at)."""
        return self.schedule.next_run(self.state.last_run_at)


class CronData(CronModel):
    """Persistent store for cron jobs."""

    version: int = 1
    jobs: list[CronJob] = Field(default_factory=list)


class CronStore:
    """Async context manager: loads on enter, always writes back on exit."""

    def __init__(self, path: Path):
        self._path = path
        self._jobs: dict[str, CronJob] = {}

    async def __aenter__(self) -> "CronStore":
        try:
            data = CronData.model_validate_json(self._path.read_text())
        except IOError as e:
            logger.warning(f"No cron store at {e}. Creating one from scratch.")
            return self
        now = now_aware()
        for j in data.jobs:
            # Drop jobs that have no future fire as of now (e.g. an `at`
            # in the past that already executed, or an `every … until`
            # that's exhausted). next_run(now) gives the first fire
            # *after* now, which is what the loop will actually wait on.
            if j.schedule.next_run(now) is None:
                continue
            self._jobs[j.id] = j
        return self

    async def __aexit__(self, *_) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(CronData(jobs=list(self._jobs.values())).model_dump_json(indent=2))

    def jobs(self) -> Iterable[CronJob]:
        return self._jobs.values()

    def get(self, jid: str) -> CronJob | None:
        return self._jobs.get(jid)

    def add(self, j: CronJob) -> None:
        self._jobs[j.id] = j

    def remove(self, jid: str) -> bool:
        return self._jobs.pop(jid, None) is not None

    def next_run_for(self, jid: str) -> datetime | None:
        j = self._jobs.get(jid)
        return j.next_run() if j else None

    def next_wake(self) -> datetime | None:
        runs = [r for j in self._jobs.values() if (r := j.next_run()) is not None]
        return min(runs) if runs else None

    def pop_due(self, now: datetime) -> list[CronJob]:
        """Return jobs whose next fire time is at or before ``now``.

        The store is unchanged; the caller is expected to call
        :meth:`executed` on each job, which advances ``last_run_at`` and
        removes any one-shot or exhausted job.
        """
        return [j for j in self._jobs.values() if (r := j.next_run()) is not None and r <= now]

    def executed(self, jid: str, now: datetime) -> None:
        """Record a fire: stamp ``last_run_at`` and drop the job if its
        schedule has no further fires."""
        if (j := self._jobs.get(jid)) is None:
            return
        j.state.last_run_at = now
        j.updated_at = now
        if j.schedule.next_run(now) is None:
            self.remove(jid)
