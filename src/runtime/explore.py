"""탐색 루프를 돌리는 시계 — 거의 쉬지 않고(JAR-046). 판단은 없다, 조립만 한다.

사용자 요청(2026-11-12): "거의 항시 돌았으면". 깨어 있는 시간(한국 시각)엔 `every`(운영은
`JARVIS_EXPLORE_EVERY`, 기본 10분)마다, 밤엔 `night_every` 마다 한 번.

- 대화 중(최근 말이 오간 지 얼마 안 됨)이면 조금 미룬다 — 같은 모델과 같은 브라우저를 쓰니
  사용자의 답이 늦어진다.
- 켜지자마자 돌지 않는다(vLLM 이 같이 뜨는 중이면 헛돈다). 마지막으로 알아본 때(저장소)를 봐서
  재시작할 때마다 바로 또 돌지 않는다.
- 한 번 터져도 루프는 계속 돈다(runtime/_GUIDE "오래 도는 태스크가 예외 한 번으로 끝나게 두지
  않는다").
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Tuple

from src.core.clock import local

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _idle(now: datetime) -> bool:
    return False


def _never() -> Optional[datetime]:
    return None


@dataclass
class ExploreLoop:
    job: Callable[[datetime], Awaitable[object]]
    every: timedelta = timedelta(minutes=10)
    night_every: timedelta = timedelta(hours=1)
    # 깨어 있는 시간(한국 시각, 시작 포함·끝 제외). 24 는 자정까지.
    day_hours: Tuple[int, int] = (8, 24)
    # 지금 대화 중인가 — 그러면 `defer` 만큼 미룬다.
    busy: Callable[[datetime], bool] = _idle
    defer: timedelta = timedelta(minutes=10)
    # 마지막으로 알아본 때(저장소) — 재시작할 때마다 바로 또 돌지 않게.
    last: Callable[[], Optional[datetime]] = _never
    startup_delay: timedelta = timedelta(minutes=10)
    retry_after: timedelta = timedelta(minutes=30)
    clock: Callable[[], datetime] = field(default=_utcnow)

    async def run_forever(self) -> None:
        await asyncio.sleep(self.startup_delay.total_seconds())
        while True:
            try:
                wait = await self.tick()
            except Exception:
                logger.exception("탐색 루프 — 돌지 판단하다 실패. %s 뒤 다시", self.retry_after)
                wait = self.retry_after
            await asyncio.sleep(max(wait.total_seconds(), 1.0))

    def gap(self, now: datetime) -> timedelta:
        start, end = self.day_hours
        return self.every if start <= local(now).hour < end else self.night_every

    async def tick(self) -> timedelta:
        """한 번 보고 때가 됐으면 알아본다. 다음에 볼 때까지 쉴 시간."""
        now = self.clock()
        gap = self.gap(now)
        last = self.last()
        if last is not None and now - last < gap:
            return gap - (now - last)
        if self.busy(now):
            logger.info("탐색 루프 — 대화 중이라 %s 미룬다", self.defer)
            return self.defer
        try:
            await self.job(now)
        except Exception:
            logger.exception("탐색 루프 실패 — %s 뒤 다시", self.retry_after)
            return self.retry_after
        return gap
