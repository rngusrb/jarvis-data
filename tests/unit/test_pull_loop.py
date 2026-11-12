"""가져오기 루프 — 차례, 실패를 견디기, 준비 안 됨."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from src.runtime.pull import NotReady, PullLoop

START = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)


@dataclass
class Fake:
    name: str
    every: timedelta
    fails: List[BaseException] = field(default_factory=list)
    calls: List[datetime] = field(default_factory=list)

    async def pull(self, now: datetime) -> str:
        self.calls.append(now)
        if self.fails:
            raise self.fails.pop(0)
        return "됨"


def test_each_puller_runs_on_its_own_schedule() -> None:
    hourly = Fake("예보", timedelta(hours=1))
    daily = Fake("공휴일", timedelta(hours=12))
    loop = PullLoop([hourly, daily])
    assert asyncio.run(loop.run_once(START)) == ["예보: 됨", "공휴일: 됨"]  # 시작하자마자 전부
    asyncio.run(loop.run_once(START + timedelta(minutes=30)))
    asyncio.run(loop.run_once(START + timedelta(hours=1)))
    assert len(hourly.calls) == 2 and len(daily.calls) == 1


def test_a_failure_is_retried_sooner_and_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = Fake("예보", timedelta(hours=1), fails=[RuntimeError("기상청 403")])
    fine = Fake("공휴일", timedelta(hours=12))
    loop = PullLoop([broken, fine], retry_after=timedelta(minutes=10))
    assert asyncio.run(loop.run_once(START)) == ["공휴일: 됨"]
    assert "기상청 403" in caplog.text and "가져오다 실패" in caplog.text
    asyncio.run(loop.run_once(START + timedelta(minutes=10)))  # 10분 뒤 다시
    assert len(broken.calls) == 2


def test_not_ready_is_a_warning_and_waits_the_full_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    waiting = Fake("예보", timedelta(hours=1), fails=[NotReady("집을 아직 모른다")])
    loop = PullLoop([waiting])
    asyncio.run(loop.run_once(START))
    assert "집을 아직 모른다" in caplog.text and "Traceback" not in caplog.text
    asyncio.run(loop.run_once(START + timedelta(minutes=30)))
    assert len(waiting.calls) == 1


def test_the_loop_keeps_going_after_a_round_fails() -> None:
    """한 바퀴가 통째로 터져도(시계가 터져도) 루프는 끝나지 않는다(runtime/_GUIDE)."""
    ticks: List[int] = []

    def clock() -> datetime:
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("시계 고장")
        if len(ticks) >= 3:
            raise asyncio.CancelledError
        return START

    loop = PullLoop([Fake("예보", timedelta(hours=1))], clock=clock, tick=0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(loop.run_forever())
    assert len(ticks) == 3
