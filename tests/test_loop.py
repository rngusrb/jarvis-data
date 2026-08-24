from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from app.loop import JarvisLoop
from src.brain.agent import JarvisAgent
from src.core.models import Observation
from src.triggers.sleep import SleepDropTrigger

BASE = datetime(2026, 8, 1, tzinfo=timezone.utc)


@dataclass
class FakeReasoner:
    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        return "어젯밤 잘 못 잤네."


@dataclass
class RecordingChannel:
    name: str = "recording"
    sent: List[str] = field(default_factory=list)
    delay: float = 0.0

    async def send(self, text: str) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(text)


@dataclass
class FakeSource:
    observations: Sequence[Observation]

    def recent(self, kind: str, since: datetime) -> Sequence[Observation]:
        return [o for o in self.observations if o.kind == kind and o.at >= since]


def _source_with_drop() -> FakeSource:
    values = [7.0, 7.2, 6.9, 7.1, 3.0]
    return FakeSource(
        [
            Observation(
                source="apple_health",
                kind="sleep_hours",
                value=v,
                at=datetime.now(timezone.utc) - timedelta(days=len(values) - i),
                meta={"segments": 15},
            )
            for i, v in enumerate(values)
        ]
    )


def _loop(channel: RecordingChannel) -> JarvisLoop:
    return JarvisLoop(
        source=_source_with_drop(),
        triggers=[SleepDropTrigger()],
        agent=JarvisAgent(reasoner=FakeReasoner()),
        channel=channel,
    )


def test_감지되면_발송한다() -> None:
    channel = RecordingChannel()
    assert asyncio.run(_loop(channel).run_once()) == 1
    assert len(channel.sent) == 1


def test_같은_신호를_두_번_말하지_않는다() -> None:
    """두 번째 실행은 쿨다운에 막혀야 한다."""
    channel = RecordingChannel()
    loop = _loop(channel)

    async def twice() -> None:
        await loop.run_once()
        await loop.run_once()

    asyncio.run(twice())
    assert len(channel.sent) == 1


def test_동시에_들어와도_한_번만_말한다() -> None:
    """주기 루프와 수집 훅이 겹치는 상황.

    게이트는 발송에 **성공한 뒤에야** 쿨다운을 기록하므로, 잠금이 없으면
    나란히 달리는 두 실행이 둘 다 게이트를 통과해 같은 말을 두 번 한다.
    """
    channel = RecordingChannel(delay=0.05)
    loop = _loop(channel)

    async def together() -> None:
        await asyncio.gather(loop.run_once(), loop.run_once())

    asyncio.run(together())
    assert len(channel.sent) == 1, f"중복 발화: {channel.sent}"


def test_발송이_터져도_루프는_살아있다() -> None:
    @dataclass
    class BrokenChannel:
        name: str = "broken"

        async def send(self, text: str) -> None:
            raise RuntimeError("텔레그램 죽음")

    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[SleepDropTrigger()],
        agent=JarvisAgent(reasoner=FakeReasoner()),
        channel=BrokenChannel(),
    )
    assert asyncio.run(loop.run_once()) == 0
