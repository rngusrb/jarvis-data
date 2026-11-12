"""서버가 바깥에서 가져오는 것 — 정한 간격마다(기상청 예보, 공휴일).

폰·맥이 보내는 수집(push)과 달리 서버가 직접 묻는다. 무엇을 어떻게 가져올지는 섹터가 정하고
(`Puller` — DEV_GUIDE "서버에서 pull 이 되는 소스는 섹터에"), 언제 돌릴지와 실패를 견디는 일은
여기가 맡는다. 하나가 터져도 나머지는 돌고, 루프는 예외 한 번으로 끝나지 않는다(runtime/_GUIDE).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Protocol, Sequence

logger = logging.getLogger(__name__)


class NotReady(Exception):
    """가져올 준비가 안 됨(예: 날씨를 받을 집을 아직 모른다). 실패가 아니라 이유 — 경고 한 줄로
    남긴다."""


class Puller(Protocol):
    name: str
    every: timedelta

    async def pull(self, now: datetime) -> str:
        """한 번 가져온다. 한 일을 한 줄로 돌려준다(로그용). 실패는 예외로."""
        ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PullLoop:
    pullers: Sequence[Puller]
    clock: Callable[[], datetime] = _now
    # 몇 초마다 차례가 됐는지 보나. 가져오는 간격(`Puller.every`)은 이보다 훨씬 길다.
    tick: float = 60.0
    # 실패하면 이만큼 뒤 다시(간격이 더 짧으면 간격대로).
    retry_after: timedelta = timedelta(minutes=10)
    _due: Dict[str, datetime] = field(default_factory=dict)

    async def run_once(self, now: datetime) -> List[str]:
        """차례가 된 것을 돌린다. 시작하자마자는 전부 차례다."""
        done: List[str] = []
        for puller in self.pullers:
            if now < self._due.get(puller.name, now):
                continue
            try:
                said = await puller.pull(now)
            except NotReady as why:
                logger.warning("%s — 못 가져옴: %s", puller.name, why)
                self._due[puller.name] = now + puller.every
                continue
            except Exception:
                logger.exception("%s — 가져오다 실패, %s 뒤 다시", puller.name, self._retry(puller))
                self._due[puller.name] = now + self._retry(puller)
                continue
            logger.info("%s — %s", puller.name, said)
            done.append(f"{puller.name}: {said}")
            self._due[puller.name] = now + puller.every
        return done

    def _retry(self, puller: Puller) -> timedelta:
        return min(self.retry_after, puller.every)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once(self.clock())
            except Exception:  # 차례 계산까지 감싼다 — 루프가 조용히 끝나면 아무도 모른다
                logger.exception("가져오기 루프 한 바퀴가 실패했다 — 다음 바퀴에 다시")
            await asyncio.sleep(self.tick)
