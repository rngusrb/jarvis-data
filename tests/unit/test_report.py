"""보고서 — 넓게 모아 쓰고, 기록 읽는 법을 지시문에 적는다."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from src.brain.context import ContextBlock
from src.brain.report import FAILED, SYSTEM_PROMPT, Reporter
from src.core.models import Insight

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)


@dataclass
class Scripted:
    reply: str
    prompts: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.prompts.append(prompt)
        return self.reply


@dataclass
class Fixed:
    label: str
    body: str
    name: str = "fixed"
    seen: List[Optional[Insight]] = field(default_factory=list)

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        self.seen.append(insight)
        return ContextBlock(label=self.label, body=self.body)


def test_the_report_reads_every_provider_without_a_signal() -> None:
    """보고서는 신호가 아니라 사람에 대한 것이다.

    가짜 신호를 만들어 넘기지 않는다(brain/_GUIDE).
    """
    topics = Fixed("요즘 관심사(주제 나무)", "- 취업 — 꾸준")
    days = Fixed("최근 하루 요약", "11-05(목) 06:54~ (지금까지)")
    reasoner = Scripted("  # 요즘의 너\n...  ")
    text = asyncio.run(Reporter(reasoner=reasoner, providers=[topics, days]).write(NOW))

    assert text == "# 요즘의 너\n..."
    assert topics.seen == [None] and days.seen == [None]
    prompt = reasoner.prompts[0]
    assert "지금: 2026-11-05(목) 12:00" in prompt and "- 취업 — 꾸준" in prompt


def test_the_instructions_say_how_to_read_the_records() -> None:
    """한 번 써 봤더니 첫 활동을 기상으로, 근거가 있던 날을 연속 일수로 읽었다(2026-11-05).

    하루가 언제 바뀌는지는 섹터의 사실이라 지시문이 아니라 요약 묶음이 들고 온다(리뷰).
    """
    from src.sectors.interest.digest import DAY_STARTS, READING

    assert "읽는 법" in SYSTEM_PROMPT and "새벽" not in SYSTEM_PROMPT
    assert '"근거가 있던 날"은 날의 수다' in SYSTEM_PROMPT
    assert '"기록 없음"인 날은 기록이 없는 것' in SYSTEM_PROMPT
    assert f"새벽 {DAY_STARTS.hour}시" in READING and "일어난" in READING
    # 텔레그램은 마크다운을 안 그린다 — ** 와 # 가 날글로 보였다.
    assert "마크다운을 그리지 않는다" in SYSTEM_PROMPT


def test_a_failure_still_answers_the_user(caplog: object) -> None:
    """두뇌에 못 닿으면 예외가 듣기 루프에서 삼켜져 사용자는 아무 답도 못 받았다(리뷰 재현)."""
    from src.brain.report import UNREACHABLE

    @dataclass
    class Down:
        async def ask(self, prompt: str, system: Optional[str] = None) -> str:
            raise ConnectionError("데스크탑 꺼짐")

    assert asyncio.run(Reporter(reasoner=Down(), providers=[]).write(NOW)) == UNREACHABLE


def test_an_empty_answer_is_not_sent_as_is() -> None:
    assert asyncio.run(Reporter(reasoner=Scripted("  "), providers=[]).write(NOW)) == FAILED
