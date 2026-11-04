"""하루 한 번 도는 일 — 지금은 회고.

정한 시각(한국 시간)에 돈다. 그 시각에 서버가 꺼져 있었으면(윈도우로 부팅한 날) 켜진 뒤
따라잡는다 — 데스크탑은 며칠씩 꺼질 때가 있어서, 시각에만 걸면 그런 날은 회고가 통째로 빠진다.

"오늘 돌았나"는 메모리가 아니라 일이 남긴 기록(`last_run`)으로 본다. 재시작할 때마다 또 돌면
배포하는 날마다 회고가 두 번 돈다. 성공한 실행만 센다 — 실패를 돈 것으로 치면 그날은 다시 안 돈다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Awaitable, Callable, Dict, Optional

from src.core.clock import KST, local

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DailyLoop:
    job: Callable[[datetime], Awaitable[object]]
    # 하루 중 언제(한국 시간).
    at: time
    # 마지막으로 성공한 실행이 '지금'으로 본 때.
    last_run: Callable[[], Optional[datetime]]
    name: str = "daily"
    # 켜지자마자 돌지 않는다. vLLM 이 같이 뜨는 중이면 첫 시도가 헛돈다.
    startup_delay: timedelta = timedelta(minutes=5)
    retry_after: timedelta = timedelta(minutes=30)
    # 한 회차에 몇 번까지 시도하나. 응답을 계속 못 읽는 날 30분마다 GPU 를 태우지 않게.
    attempts: int = 3
    clock: Callable[[], datetime] = field(default=_utcnow)

    def slot(self, now: datetime) -> datetime:
        """`now` 이전(같거나)의 가장 최근 정해진 시각."""
        today = datetime.combine(local(now).date(), self.at, tzinfo=KST)
        return today if today <= now else today - timedelta(days=1)

    def due(self, now: datetime, last: Optional[datetime]) -> datetime:
        """다음에 돌 때. 이번 회차를 아직 안 돌았으면 지금이다."""
        slot = self.slot(now)
        if last is None or last < slot:
            return now
        return slot + timedelta(days=1)

    async def run_forever(self) -> None:
        await asyncio.sleep(self.startup_delay.total_seconds())
        tried: Dict[datetime, int] = {}
        while True:
            try:
                wait = await self._tick(tried)
                if wait is None:
                    continue
            except Exception:
                # 기록을 읽다 터진 것(database is locked 등). 여기서 태스크가 끝나면 서버가 도는
                # 동안 아무 로그 없이 회고가 영영 멈춘다 — 끄는 쪽만 태스크를 거두기 때문이다
                # (리뷰 재현).
                logger.exception("%s — 돌지 판단하다 실패. %s 뒤 다시", self.name, self.retry_after)
                wait = self.retry_after
            await asyncio.sleep(max(wait.total_seconds(), 1.0))

    async def _tick(self, tried: Dict[datetime, int]) -> Optional[timedelta]:
        """한 번 보고 필요하면 돈다. 다음에 볼 때까지 쉴 시간을 돌려준다.

        방금 돌았으면 None — 쉬지 않고 바로 다음 회차를 계산한다.
        """
        now = self.clock()
        due = self.due(now, self.last_run())
        if due > now:
            return due - now
        slot = self.slot(now)
        if tried.get(slot, 0) >= self.attempts:
            logger.error(
                "%s — %s 회차를 %d번 실패해 다음 회차까지 쉰다",
                self.name,
                f"{local(slot):%m-%d %H:%M}",
                self.attempts,
            )
            return slot + timedelta(days=1) - now
        tried[slot] = tried.get(slot, 0) + 1
        try:
            await self.job(now)
        except Exception:
            logger.exception("%s 실패 — %s 뒤 다시", self.name, self.retry_after)
            return self.retry_after
        last = self.last_run()
        if last is not None and last >= slot:
            return None
        # 돌았다는데 기록이 없으면 바로 또 돌게 된다. 다음 회차까지 쉰다.
        logger.error("%s — 돌았는데 실행 기록이 없다. 다음 회차까지 쉰다", self.name)
        return slot + timedelta(days=1) - now
