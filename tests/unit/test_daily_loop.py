"""하루 한 번 도는 일 — 정한 시각, 놓쳤으면 따라잡기, 실패는 다시, 재시작해도 두 번 안 돌기."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import List, Optional

import pytest

from src.core.clock import KST
from src.runtime.daily import DailyLoop

AT = time(5, 30)


def _kst(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=KST)


def _loop(last: Optional[datetime] = None) -> DailyLoop:
    async def job(now: datetime) -> None:
        return None

    return DailyLoop(job=job, at=AT, last_run=lambda: last)


def test_runs_at_the_set_time_of_day() -> None:
    loop = _loop()
    yesterday = _kst(2, 5, 30)
    assert loop.due(_kst(3, 4, 0), yesterday) == _kst(3, 5, 30)
    assert loop.due(_kst(3, 5, 30), yesterday) == _kst(3, 5, 30)


def test_catches_up_after_being_off_at_that_time() -> None:
    """데스크탑이 윈도우로 켜져 있던 날은 05:30 을 놓친다. 리눅스로 켜지면 바로 돈다."""
    loop = _loop()
    now = _kst(3, 13, 0)
    assert loop.due(now, _kst(2, 5, 30)) == now
    assert loop.due(now, None) == now


def test_a_restart_after_todays_run_waits_for_tomorrow() -> None:
    """재시작할 때마다 또 돌면 배포하는 날마다 회고가 두 번 돈다."""
    loop = _loop()
    assert loop.due(_kst(3, 13, 0), _kst(3, 5, 31)) == _kst(4, 5, 30)


def test_before_the_time_counts_yesterdays_slot() -> None:
    """새벽 3시에 켜졌고 어제 회고를 못 돌았으면 지금 돈다 — 다음 05:30 까지 미루지 않는다."""
    loop = _loop()
    assert loop.slot(_kst(3, 3, 0)) == _kst(2, 5, 30)
    assert loop.due(_kst(3, 3, 0), _kst(1, 5, 30)) == _kst(3, 3, 0)


class _Stop(Exception):
    pass


def _drive(loop: DailyLoop, monkeypatch: pytest.MonkeyPatch, sleeps: int) -> List[float]:
    """시계를 멈춰 두고 잠든 시간만 모은다. 정해진 횟수만큼 잠들면 멈춘다."""
    waits: List[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) > sleeps:
            raise _Stop

    monkeypatch.setattr("src.runtime.daily.asyncio.sleep", fake_sleep)
    with pytest.raises(_Stop):
        asyncio.run(loop.run_forever())
    return waits


def test_a_failed_run_is_retried_and_then_given_up_until_the_next_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _kst(3, 13, 0)
    calls: List[datetime] = []

    async def job(at: datetime) -> None:
        calls.append(at)
        raise RuntimeError("vLLM 이 아직 안 떴다")

    loop = DailyLoop(job=job, at=AT, last_run=lambda: None, clock=lambda: now, attempts=2)
    waits = _drive(loop, monkeypatch, sleeps=3)

    assert len(calls) == 2
    startup = loop.startup_delay.total_seconds()
    retry = loop.retry_after.total_seconds()
    assert waits == [startup, retry, retry, (_kst(4, 5, 30) - now).total_seconds()]


def test_a_successful_run_sleeps_until_the_next_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    now = _kst(3, 13, 0)
    ran: List[datetime] = []

    async def job(at: datetime) -> None:
        ran.append(at)

    loop = DailyLoop(job=job, at=AT, last_run=lambda: ran[-1] if ran else None, clock=lambda: now)
    waits = _drive(loop, monkeypatch, sleeps=1)

    assert ran == [now]
    assert waits[1] == (_kst(4, 5, 30) - now).total_seconds()


def test_a_run_that_leaves_no_record_does_not_spin(monkeypatch: pytest.MonkeyPatch) -> None:
    """돌았다는데 기록이 없으면 바로 또 돌게 된다. 다음 회차까지 쉰다."""
    now = _kst(3, 13, 0)
    ran: List[datetime] = []

    async def job(at: datetime) -> None:
        ran.append(at)

    loop = DailyLoop(job=job, at=AT, last_run=lambda: None, clock=lambda: now)
    waits = _drive(loop, monkeypatch, sleeps=1)

    assert ran == [now]
    assert waits[1] == (_kst(4, 5, 30) - now).total_seconds()


def test_slot_is_in_korean_time_whatever_the_clock_zone() -> None:
    loop = _loop()
    utc_now = datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)  # 한국 11-03 06:00
    assert loop.slot(utc_now) == _kst(3, 5, 30)
    assert loop.due(utc_now, _kst(3, 5, 30) - timedelta(seconds=1)) == utc_now


def test_a_failing_record_read_does_not_end_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """기록을 읽다 한 번 터졌다고 태스크가 끝나면 안 된다.

    서버가 도는 동안 로그 없이 회고가 멈춘다(리뷰 재현).
    """
    now = _kst(3, 13, 0)
    reads: List[int] = []
    ran: List[datetime] = []

    def last_run() -> Optional[datetime]:
        reads.append(1)
        if len(reads) == 1:
            raise sqlite3.OperationalError("database is locked")
        return ran[-1] if ran else None

    async def job(at: datetime) -> None:
        ran.append(at)

    loop = DailyLoop(job=job, at=AT, last_run=last_run, clock=lambda: now)
    waits = _drive(loop, monkeypatch, sleeps=2)

    assert waits[1] == loop.retry_after.total_seconds()
    assert ran == [now]
