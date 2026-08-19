"""자비스 루프 — 이 프로덕트의 심장.

수집 파이프라인(src/pipelines)이 도는 "배치 루프"와는 별개로 돌아간다.
배치는 데이터를 채우고, 이 루프는 채워진 데이터를 보고 **먼저 말을 건다**.

    최근 관측 조회 → 트리거 감지 → 에이전트 판단 → 채널 발송

이 파일에는 판단 로직이 없다. 전부 조립만 한다 — 판단은 src/brain,
감지는 src/triggers, 발송은 src/channels가 각자 책임진다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Sequence

from src.brain.agent import JarvisAgent
from src.channels.base import Channel
from src.core.models import ObservationSource
from src.triggers.base import Trigger

logger = logging.getLogger(__name__)


async def run_once(
    source: ObservationSource,
    triggers: Sequence[Trigger],
    agent: JarvisAgent,
    channel: Channel,
) -> int:
    """한 주기를 돈다. 실제로 발송한 메시지 수를 돌려준다."""
    now = datetime.now(timezone.utc)
    sent = 0

    for trigger in triggers:
        window = source.recent(trigger.kind, since=now - trigger.lookback)
        insight = trigger.check(window)
        if insight is None:
            continue

        message = await agent.consider(insight, now)
        if message is None:
            logger.debug("말 안 걸기로 함: %s", insight.trigger)
            continue

        try:
            await channel.send(message)
        except Exception:
            # 발송 실패는 루프를 죽이지 않는다. 다음 주기에 다시 시도된다.
            logger.exception("발송 실패 (%s): %s", channel.name, insight.trigger)
            continue

        agent.confirm_spoken(insight, now, message)
        sent += 1

    return sent


async def run_forever(
    source: ObservationSource,
    triggers: Sequence[Trigger],
    agent: JarvisAgent,
    channel: Channel,
    interval_sec: int,
) -> None:
    while True:
        try:
            sent = await run_once(source, triggers, agent, channel)
            logger.info("주기 완료 — %d건 발송", sent)
        except Exception:
            # 한 주기가 통째로 터져도 자비스는 계속 살아 있어야 한다.
            logger.exception("주기 실행 중 예외")
        await asyncio.sleep(interval_sec)
