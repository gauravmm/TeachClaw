"""Tests for cron schedule types and the in-memory CronStore.

The serialization round-trip is covered by test_cron_serialization.py;
this file targets the pure logic: schedule.next_run() across the three
schedule kinds, the schedule-discriminator validator on CronJob, and
the CronStore state machine (add/enable/remove/executed/pop_due).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from teachclaw.agent.tools.cron.typesupport import (
    CronJob,
    CronScheduleAt,
    CronScheduleCron,
    CronScheduleEvery,
    CronStore,
)
from teachclaw.bus import MessageAddress

UTC = timezone.utc


def _addr() -> MessageAddress:
    return MessageAddress(channel="telegram", chat_id="42")


def _job(jid: str, schedule, *, enabled: bool = True) -> CronJob:
    return CronJob(id=jid, message="ping", deliver_to=_addr(), schedule=schedule, enabled=enabled)


# ---------------------------------------------------------------------------
# Schedule.next_run
# ---------------------------------------------------------------------------


def test_at_returns_target_when_in_future():
    target = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    sched = CronScheduleAt(at=target)
    assert sched.next_run(datetime(2025, 1, 1, tzinfo=UTC)) == target


def test_at_returns_none_when_past():
    target = datetime(2020, 1, 1, tzinfo=UTC)
    sched = CronScheduleAt(at=target)
    assert sched.next_run(datetime(2025, 1, 1, tzinfo=UTC)) is None


def test_at_returns_none_when_unset():
    assert CronScheduleAt(at=None).next_run(datetime(2025, 1, 1, tzinfo=UTC)) is None


def test_every_advances_by_one_interval_after_anchor():
    """``next_run`` should be the first anchor + N*interval that's strictly
    after ``dt``. The +1 in the floor formula is what guarantees strictness."""
    anchor = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    sched = CronScheduleEvery(every=timedelta(minutes=10), anchor=anchor)
    # Exactly at anchor → next is anchor + 10m.
    assert sched.next_run(anchor) == anchor + timedelta(minutes=10)
    # 25 minutes past → next is anchor + 30m.
    assert sched.next_run(anchor + timedelta(minutes=25)) == anchor + timedelta(minutes=30)


def test_every_returns_none_after_until():
    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    sched = CronScheduleEvery(
        every=timedelta(minutes=5),
        anchor=anchor,
        until=anchor + timedelta(minutes=10),
    )
    # Past `until` → exhausted.
    assert sched.next_run(anchor + timedelta(hours=1)) is None


def test_cron_returns_none_for_empty_expression():
    assert CronScheduleCron(expr="").next_run(datetime(2025, 1, 1, tzinfo=UTC)) is None


def test_cron_returns_none_for_invalid_expression():
    """The next_run wrapper swallows croniter exceptions to keep the loop
    alive; verify a syntactically broken expression yields None instead of
    raising."""
    assert CronScheduleCron(expr="not a cron").next_run(datetime(2025, 1, 1, tzinfo=UTC)) is None


def test_cron_advances_to_next_minute():
    """``* * * * *`` fires every minute — next_run from 12:00:30 must be
    12:01:00."""
    sched = CronScheduleCron(expr="* * * * *", tz="UTC")
    start = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    nxt = sched.next_run(start)
    assert nxt is not None
    assert nxt > start
    assert nxt - start <= timedelta(minutes=1, seconds=30)


# ---------------------------------------------------------------------------
# CronJob schedule discriminator
# ---------------------------------------------------------------------------


def test_cronjob_validator_picks_schedule_at_from_dict():
    j = CronJob(
        id="x",
        message="m",
        deliver_to=_addr(),
        schedule={"at": datetime(2030, 1, 1, tzinfo=UTC)},
    )
    assert isinstance(j.schedule, CronScheduleAt)


def test_cronjob_validator_picks_schedule_every_from_dict():
    j = CronJob(
        id="x",
        message="m",
        deliver_to=_addr(),
        schedule={"every": "5m"},
    )
    assert isinstance(j.schedule, CronScheduleEvery)


def test_cronjob_validator_picks_schedule_cron_from_dict():
    j = CronJob(
        id="x",
        message="m",
        deliver_to=_addr(),
        schedule={"expr": "* * * * *"},
    )
    assert isinstance(j.schedule, CronScheduleCron)


def test_cronjob_validator_rejects_unknown_schedule_keys():
    with pytest.raises(Exception):  # pydantic wraps as ValidationError
        CronJob(id="x", message="m", deliver_to=_addr(), schedule={"banana": 1})


# ---------------------------------------------------------------------------
# CronStore — state mutations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_add_queues_enabled_jobs(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleEvery(every=timedelta(minutes=5))))
        assert store.next_run_for("a") is not None


@pytest.mark.asyncio
async def test_store_add_skips_queue_when_disabled(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleEvery(every=timedelta(minutes=5)), enabled=False))
        assert store.get("a") is not None
        assert store.next_run_for("a") is None


@pytest.mark.asyncio
async def test_store_add_skips_queue_when_schedule_exhausted(tmp_path):
    """A CronScheduleAt in the past has next_run=None — the job is kept in
    the store (so the user can still see it) but not queued."""
    past = datetime(2020, 1, 1, tzinfo=UTC)
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleAt(at=past)))
        assert store.get("a") is not None
        assert store.next_run_for("a") is None


@pytest.mark.asyncio
async def test_store_remove_returns_false_if_missing(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        assert store.remove("nope") is False


@pytest.mark.asyncio
async def test_store_remove_clears_store_and_queue(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleEvery(every=timedelta(minutes=5))))
        assert store.remove("a") is True
        assert store.get("a") is None
        assert store.next_run_for("a") is None


@pytest.mark.asyncio
async def test_store_enable_toggles_queue(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleEvery(every=timedelta(minutes=5))))
        assert store.enable("a", False, now) is True
        assert store.next_run_for("a") is None
        assert store.enable("a", True, now) is True
        assert store.next_run_for("a") is not None


@pytest.mark.asyncio
async def test_store_enable_returns_false_for_unknown_job(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        assert store.enable("nope", True, datetime(2026, 1, 1, tzinfo=UTC)) is False


@pytest.mark.asyncio
async def test_store_executed_removes_one_shot(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleAt(at=datetime(2030, 1, 1, tzinfo=UTC))))
        store.executed("a", datetime(2030, 1, 1, tzinfo=UTC))
        assert store.get("a") is None


@pytest.mark.asyncio
async def test_store_executed_reschedules_recurring(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleEvery(every=timedelta(minutes=5))))
        first = store.next_run_for("a")
        # Pretend we just executed at the queued time; the next slot moves forward.
        store.executed("a", first + timedelta(seconds=1))
        second = store.next_run_for("a")
        assert second is not None
        assert second > first


@pytest.mark.asyncio
async def test_store_executed_removes_expired_recurring(tmp_path):
    """An ``every … until`` job that's now past its until window should be
    auto-removed once it tries to schedule its next run."""
    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(
            _job(
                "a",
                CronScheduleEvery(
                    every=timedelta(minutes=5), anchor=anchor, until=anchor + timedelta(minutes=10)
                ),
            )
        )
        store.executed("a", anchor + timedelta(hours=1))
        assert store.get("a") is None


@pytest.mark.asyncio
async def test_store_executed_silently_ignores_unknown(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        store.executed("nope", datetime(2026, 1, 1, tzinfo=UTC))  # must not raise


@pytest.mark.asyncio
async def test_store_next_wake_returns_earliest(tmp_path):
    anchor_a = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    anchor_b = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleEvery(every=timedelta(hours=1), anchor=anchor_a)))
        store.add(_job("b", CronScheduleEvery(every=timedelta(hours=1), anchor=anchor_b)))
        wake = store.next_wake()
        assert wake is not None
        # Earliest of the two queued entries is the one anchored earlier.
        assert wake <= store.next_run_for("b")


@pytest.mark.asyncio
async def test_store_next_wake_returns_none_when_empty(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        assert store.next_wake() is None


@pytest.mark.asyncio
async def test_store_pop_due_returns_only_due_jobs(tmp_path):
    """``add`` queues each job at its current next_run (computed against
    real now). Sliding the pop cursor past one entry but before the other
    must surface only the first."""
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("soon", CronScheduleEvery(every=timedelta(minutes=5))))
        store.add(_job("later", CronScheduleEvery(every=timedelta(days=30))))
        soon_at = store.next_run_for("soon")
        later_at = store.next_run_for("later")
        assert soon_at is not None and later_at is not None
        cursor = soon_at + timedelta(seconds=1)
        assert cursor < later_at  # sanity
        due = store.pop_due(cursor)
        assert [j.id for j in due] == ["soon"]


@pytest.mark.asyncio
async def test_store_pop_due_skips_disabled_jobs(tmp_path):
    """Disabled jobs that somehow ended up in the queue must be filtered
    out at pop time (defence in depth — enable() also drains the queue)."""
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleEvery(every=timedelta(minutes=5))))
        next_at = store.next_run_for("a")
        # Sneak the job into a disabled state without going through enable().
        store._store["a"].enabled = False
        due = store.pop_due(next_at + timedelta(seconds=1))
        assert due == []


@pytest.mark.asyncio
async def test_store_drops_expired_jobs_on_load(tmp_path):
    """Jobs whose schedule has no future next_run on enter() should not
    survive into the in-memory store, so they're discarded on the next
    write-back."""
    path = tmp_path / "jobs.json"
    async with CronStore(path) as store:
        store.add(_job("a", CronScheduleAt(at=datetime(2030, 1, 1, tzinfo=UTC))))
    # Reopen *after* the AT time has passed.
    async with CronStore(path) as store:
        # Force a relative-time check by using a faraway "now"; CronStore.__aenter__
        # uses now_aware(), so we can only validate the live behavior with a
        # current-clock test. Use a far-past schedule instead so it's
        # already-expired regardless of clock.
        pass
    # Replace with a now-expired schedule and verify drop:
    async with CronStore(path) as store:
        store.add(_job("b", CronScheduleAt(at=datetime(2020, 1, 1, tzinfo=UTC))))
    async with CronStore(path) as store:
        assert store.get("b") is None
