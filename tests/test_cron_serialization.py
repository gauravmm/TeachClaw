import asyncio
import json
from datetime import datetime, timedelta

import pytest

from teachclaw.agent.tools.cron.tool import CronTool
from teachclaw.agent.tools.cron.typesupport import (
    CronJob,
    CronJobState,
    CronScheduleEvery,
    CronStore,
)
from teachclaw.bus import MessageAddress, MessageBus, SystemMessageEvent


def _address() -> MessageAddress:
    return MessageAddress(channel="telegram", chat_id="123456")


@pytest.mark.asyncio
async def test_last_run_at_round_trips_as_iso_timestamp(tmp_path) -> None:
    last_run_at = datetime(2026, 3, 10, 8, 56, 16).astimezone()
    job = CronJob(
        id="aaa00001",
        message="heartbeat",
        deliver_to=_address(),
        schedule=CronScheduleEvery(every=timedelta(minutes=30)),
        state=CronJobState(last_run_at=last_run_at, last_status="ok"),
    )
    store_path = tmp_path / "jobs.json"

    async with CronStore(store_path) as store:
        store.add(job)

    data = json.loads(store_path.read_text())
    assert data["jobs"][0]["state"]["last_run_at"] == last_run_at.isoformat(timespec="seconds")

    async with CronStore(store_path) as store:
        jobs = list(store.jobs())

    assert len(jobs) == 1
    assert jobs[0].state.last_run_at == last_run_at.replace(microsecond=0)


@pytest.mark.asyncio
async def test_schedule_every_serializes_as_duration_string(tmp_path) -> None:
    job = CronJob(
        id="aaa00002",
        message="heartbeat",
        deliver_to=_address(),
        schedule=CronScheduleEvery(every=timedelta(minutes=30)),
    )
    store_path = tmp_path / "jobs.json"

    async with CronStore(store_path) as store:
        store.add(job)

    data = json.loads(store_path.read_text())
    assert data["jobs"][0]["schedule"]["every"] == "30m"


@pytest.mark.asyncio
async def test_last_run_at_accepts_iso_datetime(tmp_path) -> None:
    last_run = datetime(2026, 3, 10, 9, 14, 47).astimezone()
    iso = last_run.isoformat(timespec="seconds")
    store_path = tmp_path / "jobs.json"
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "bbb00001",
                        "message": "legacy",
                        "deliver_to": {"channel": "telegram", "chat_id": "123456"},
                        "state": {
                            "last_run_at": iso,
                            "last_status": "ok",
                            "last_error": None,
                        },
                        "enabled": True,
                        "schedule": {"every": 1800.0, "anchor": iso, "until": None},
                        "created_at": iso,
                        "updated_at": iso,
                    }
                ],
            }
        )
    )

    async with CronStore(store_path) as store:
        jobs = list(store.jobs())

    assert len(jobs) == 1
    assert jobs[0].state.last_run_at == last_run.replace(microsecond=0)


@pytest.mark.asyncio
async def test_execute_job_records_timestamp_state(tmp_path) -> None:
    bus = MessageBus()
    address = _address()
    bus.inbound[address] = asyncio.Queue()
    tool = CronTool(store_path=tmp_path / "jobs.json", bus=bus)
    job = CronJob(
        id="ccc00001",
        message="tick",
        deliver_to=address,
        schedule=CronScheduleEvery(every=timedelta(minutes=5)),
        state=CronJobState(),
    )

    async with CronStore(tool._store_path) as store:
        tool._store = store
        store.add(job)
        before = datetime.now().astimezone()
        await tool._execute_job(job)
        after = datetime.now().astimezone()

        event = await bus.inbound[address].get()

        assert isinstance(event, SystemMessageEvent)
        assert event.content == "tick"
        assert job.state.last_status == "ok"
        assert job.state.last_error is None
        assert job.state.last_run_at is not None
        assert before <= job.state.last_run_at <= after

    tool._store = None
