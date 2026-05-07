"""Tests for cron schedule types and the in-memory CronStore.

The serialization round-trip is covered by test_cron_serialization.py;
this file targets the pure logic: schedule.next_run() across the two
schedule kinds, the schedule-discriminator validator on CronJob, and
the CronStore state machine (add/remove/executed/pop_due).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from teachclaw.agent.tools.cron.typesupport import (
    CronJob,
    CronScheduleAt,
    CronScheduleEvery,
    CronStore,
)
from teachclaw.bus import MessageAddress

UTC = timezone.utc


def _addr() -> MessageAddress:
    return MessageAddress(channel="telegram", chat_id="42")


def _job(jid: str, schedule) -> CronJob:
    return CronJob(id=jid, message="ping", deliver_to=_addr(), schedule=schedule)


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


def test_at_first_run_when_ref_is_none():
    """``ref is None`` (never fired) returns the scheduled time."""
    target = datetime(2030, 1, 1, tzinfo=UTC)
    assert CronScheduleAt(at=target).next_run(None) == target


def test_every_first_run_at_anchor():
    """``ref is None`` or ``ref < anchor`` returns the anchor — i.e. the
    first fire is at anchor itself, not anchor + every."""
    anchor = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    sched = CronScheduleEvery(every=timedelta(minutes=10), anchor=anchor)
    assert sched.next_run(None) == anchor
    assert sched.next_run(anchor - timedelta(minutes=1)) == anchor


def test_every_advances_strictly_after_ref_past_anchor():
    """Once ``ref >= anchor``, ``next_run`` returns the first slot strictly
    after ``ref``."""
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


def test_cronjob_validator_rejects_unknown_schedule_keys():
    with pytest.raises(Exception):  # pydantic wraps as ValidationError
        CronJob(id="x", message="m", deliver_to=_addr(), schedule={"banana": 1})


# ---------------------------------------------------------------------------
# CronStore — state mutations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_add_keeps_job(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleEvery(every=timedelta(minutes=5))))
        assert store.next_run_for("a") is not None


@pytest.mark.asyncio
async def test_store_past_at_fires_then_self_removes(tmp_path):
    """A past one-shot fires on the next tick (next_run_for returns ``at``
    even when ``at < now``), then self-removes via ``executed``."""
    past = datetime(2020, 1, 1, tzinfo=UTC)
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleAt(at=past)))
        assert store.next_run_for("a") == past
        store.executed("a", datetime.now().astimezone())
        assert store.get("a") is None


@pytest.mark.asyncio
async def test_store_remove_returns_false_if_missing(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        assert store.remove("nope") is False


@pytest.mark.asyncio
async def test_store_remove_clears_store(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleEvery(every=timedelta(minutes=5))))
        assert store.remove("a") is True
        assert store.get("a") is None
        assert store.next_run_for("a") is None


@pytest.mark.asyncio
async def test_store_executed_removes_one_shot(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleAt(at=datetime(2030, 1, 1, tzinfo=UTC))))
        store.executed("a", datetime(2030, 1, 1, tzinfo=UTC))
        assert store.get("a") is None


@pytest.mark.asyncio
async def test_store_executed_advances_recurring(tmp_path):
    """``executed`` stamps ``last_run_at``, which is what ``next_run`` keys
    off — so next_run_for advances to the slot after the fire time."""
    anchor = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("a", CronScheduleEvery(every=timedelta(minutes=5), anchor=anchor)))
        assert store.next_run_for("a") == anchor
        store.executed("a", anchor + timedelta(seconds=1))
        nxt = store.next_run_for("a")
        assert nxt is not None
        assert nxt > anchor


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
        assert wake <= store.next_run_for("b")


@pytest.mark.asyncio
async def test_store_next_wake_returns_none_when_empty(tmp_path):
    async with CronStore(tmp_path / "jobs.json") as store:
        assert store.next_wake() is None


@pytest.mark.asyncio
async def test_store_pop_due_returns_only_due_jobs(tmp_path):
    """``pop_due`` returns jobs whose next fire time is at or before the
    cursor; jobs with later next fires are skipped."""
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    async with CronStore(tmp_path / "jobs.json") as store:
        store.add(_job("soon", CronScheduleEvery(every=timedelta(minutes=5), anchor=now)))
        store.add(
            _job(
                "later",
                CronScheduleEvery(every=timedelta(days=30), anchor=now + timedelta(days=30)),
            )
        )
        cursor = now + timedelta(seconds=1)
        due = store.pop_due(cursor)
        assert [j.id for j in due] == ["soon"]


@pytest.mark.asyncio
async def test_store_drops_expired_jobs_on_load(tmp_path):
    """Jobs whose schedule has no future next_run on enter() should not
    survive into the in-memory store, so they're discarded on the next
    write-back."""
    path = tmp_path / "jobs.json"
    async with CronStore(path) as store:
        store.add(_job("b", CronScheduleAt(at=datetime(2020, 1, 1, tzinfo=UTC))))
    async with CronStore(path) as store:
        assert store.get("b") is None
