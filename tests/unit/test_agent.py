from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.brain.agent import JarvisAgent
from src.brain.gate import Gate
from src.brain.providers import ObservationTrendProvider, SpeechHistoryProvider
from src.core.models import Insight, Observation, Severity

NOW = datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc)


@dataclass
class FakeReasoner:
    """네트워크도 GPU도 타지 않는 가짜 두뇌.

    Reasoner를 프로토콜로 뽑아둔 덕에 이게 그대로 꽂힌다.
    """

    reply: str = "어젯밤 좀 못 잤네. 오늘은 무리하지 말자."
    prompts: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.prompts.append(prompt)
        return self.reply


def _insight(severity: Severity = Severity.NOTABLE, observations: tuple = ()) -> Insight:
    return Insight(
        trigger="sleep_drop",
        summary="어젯밤 수면 5.0시간, 평균보다 2.0시간 짧음",
        severity=severity,
        at=NOW,
        observations=observations,
    )


def test_평범한_신호면_말을_건다() -> None:
    agent = JarvisAgent(reasoner=FakeReasoner())
    assert asyncio.run(agent.consider(_insight(), NOW)) is not None


def test_LLM이_SKIP하면_입을_다문다() -> None:
    agent = JarvisAgent(reasoner=FakeReasoner(reply="SKIP"))
    assert asyncio.run(agent.consider(_insight(), NOW)) is None


def test_게이트에_막히면_LLM을_아예_부르지_않는다() -> None:
    """비싼 호출 앞에 싼 필터가 있는지 확인하는 테스트."""
    brain = FakeReasoner()
    agent = JarvisAgent(reasoner=brain)
    assert asyncio.run(agent.consider(_insight(Severity.INFO), NOW)) is None
    assert brain.prompts == []


def test_발송_성공을_기억해서_반복하지_않는다() -> None:
    agent = JarvisAgent(reasoner=FakeReasoner())
    insight = _insight()

    first = asyncio.run(agent.consider(insight, NOW))
    assert first is not None
    agent.confirm_spoken(insight, NOW, first)

    assert asyncio.run(agent.consider(insight, NOW + timedelta(hours=1))) is None


def test_발송_실패는_기억에_남지_않는다() -> None:
    """confirm_spoken을 안 부르면 다음 주기에 다시 시도되어야 한다."""
    agent = JarvisAgent(reasoner=FakeReasoner())
    insight = _insight()

    assert asyncio.run(agent.consider(insight, NOW)) is not None
    # 발송이 터졌다고 치고 confirm_spoken을 부르지 않는다.
    assert asyncio.run(agent.consider(insight, NOW + timedelta(minutes=30))) is not None


def test_맥락이_프롬프트에_실린다() -> None:
    brain = FakeReasoner()
    gate = Gate()
    gate.log.record("heart_rate_spike", NOW - timedelta(hours=2), "심박이 좀 높네")

    observations = tuple(
        Observation(
            source="apple_health",
            kind="sleep_hours",
            value=v,
            at=NOW - timedelta(days=4 - i),
        )
        for i, v in enumerate([7.0, 7.2, 6.9, 5.0])
    )
    agent = JarvisAgent(
        reasoner=brain,
        gate=gate,
        providers=(ObservationTrendProvider(), SpeechHistoryProvider(log=gate.log)),
    )

    asyncio.run(agent.consider(_insight(observations=observations), NOW))

    prompt = brain.prompts[0]
    assert "sleep_hours 최근 추이" in prompt
    assert "심박이 좀 높네" in prompt
