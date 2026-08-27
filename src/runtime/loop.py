"""자비스 루프 — 이 프로덕트의 심장.

수집 파이프라인(src/pipelines)이 도는 "배치 루프"와는 별개로 돌아간다.
배치는 데이터를 채우고, 이 루프는 채워진 데이터를 보고 **먼저 말을 건다**.

    최근 관측 조회 → 트리거 감지 → 에이전트 판단 → 채널 발송

루프가 도는 계기는 두 가지다.

  - 주기적으로 (기본 30분): 데이터가 **안** 들어오는 것도 신호다.
    수집이 멈춘 걸 알아채려면 시계가 계속 돌아야 한다.
  - 수집 직후: 아침에 데이터가 도착했는데 30분 뒤에 말을 걸면 늦다.

이 파일에는 판단 로직이 없다. 전부 조립만 한다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from src.brain.agent import JarvisAgent
from src.channels.base import Channel
from src.core.models import ObservationSource
from src.triggers.base import Trigger

logger = logging.getLogger(__name__)


@dataclass
class JarvisLoop:
    source: ObservationSource
    triggers: Sequence[Trigger]
    agent: JarvisAgent
    channel: Channel
    # 주기 실행과 수집 훅이 동시에 들어올 수 있다. 겹치면 같은 신호로 두 번
    # 말하게 된다 — 게이트는 **발송에 성공한 뒤에야** 쿨다운을 기록하므로,
    # 나란히 달리는 두 실행은 둘 다 게이트를 통과해버린다.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def run_once(self) -> int:
        """한 주기를 돈다. 실제로 발송한 메시지 수를 돌려준다."""
        async with self._lock:
            return await self._cycle()

    async def _cycle(self) -> int:
        now = datetime.now(timezone.utc)
        sent = 0

        for trigger in self.triggers:
            window = self.source.recent(trigger.kind, since=now - trigger.lookback)
            insight = trigger.check(window, now)
            if insight is None:
                continue

            try:
                message = await self.agent.consider(insight, now)
            except Exception:
                # 판단 실패도 발송 실패와 똑같이 다뤄야 한다. 여기서 예외가
                # 위로 새면 이 주기의 **뒤에 선 트리거들이 통째로 건너뛰어진다** —
                # 두뇌 하나가 죽었다고 수집 중단 감지까지 멎는 건 말이 안 된다.
                # 실제 사고: vLLM이 로딩되는 10분 동안 아침 신호 4건이 이렇게
                # 사라졌고, 로그에 트레이스백만 남아 아무도 모르고 지나갔다.
                logger.exception("판단 실패 — 건너뛴다: %s", insight.trigger)
                continue

            if message is None:
                logger.debug("말 안 걸기로 함: %s", insight.trigger)
                continue

            try:
                await self.channel.send(message)
            except Exception:
                # 발송 실패는 루프를 죽이지 않는다. 다음 기회에 다시 시도된다.
                logger.exception("발송 실패 (%s): %s", self.channel.name, insight.trigger)
                continue

            self.agent.confirm_spoken(insight, now, message)
            sent += 1

        return sent

    async def run_forever(self, interval_sec: int) -> None:
        while True:
            try:
                sent = await self.run_once()
                logger.info("주기 완료 — %d건 발송", sent)
            except Exception:
                # 한 주기가 통째로 터져도 자비스는 계속 살아 있어야 한다.
                logger.exception("주기 실행 중 예외")
            await asyncio.sleep(interval_sec)
