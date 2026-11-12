"""탐색 루프의 시계 — 낮엔 자주, 밤엔 드물게, 대화 중엔 미룸, 실패해도 계속(JAR-046)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.runtime.explore import ExploreLoop

DAY = datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc)  # 한국 14:00
NIGHT = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)  # 한국 03:00


class Job:
    def __init__(self, fail: bool = False) -> None:
        self.ran: List[datetime] = []
        self.fail = fail

    async def __call__(self, now: datetime) -> None:
        self.ran.append(now)
        if self.fail:
            raise RuntimeError("터짐")


def _loop(
    now: datetime, job: Job, last: Optional[datetime] = None, busy: bool = False
) -> ExploreLoop:
    return ExploreLoop(
        job=job,
        every=timedelta(minutes=10),
        night_every=timedelta(hours=1),
        busy=lambda _: busy,
        last=lambda: last,
        clock=lambda: now,
    )


def test_day_runs_every_ten_minutes_and_night_every_hour() -> None:
    job = Job()
    assert asyncio.run(_loop(DAY, job).tick()) == timedelta(minutes=10)
    assert asyncio.run(_loop(NIGHT, job).tick()) == timedelta(hours=1)
    assert job.ran == [DAY, NIGHT]


def test_it_waits_out_the_gap_after_the_last_run_even_after_a_restart() -> None:
    job = Job()
    wait = asyncio.run(_loop(DAY, job, last=DAY - timedelta(minutes=4)).tick())
    assert wait == timedelta(minutes=6) and job.ran == []


def test_it_yields_while_talking_and_keeps_going_after_a_failure() -> None:
    job = Job()
    loop = _loop(DAY, job, busy=True)
    assert asyncio.run(loop.tick()) == loop.defer and job.ran == []
    broken = Job(fail=True)
    failing = _loop(DAY, broken)
    assert asyncio.run(failing.tick()) == failing.retry_after and broken.ran == [DAY]
