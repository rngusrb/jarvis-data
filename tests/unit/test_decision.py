"""판단 답의 형식.

말할지와 할 말을 한 덩어리 자유 글로 받으면 판단 과정이 그대로 사용자에게 간다.
2026-10-15 아침 실제로 그랬다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from src.brain.agent import JarvisAgent
from src.brain.prompts import Decision, parse_decision
from src.core.models import Insight, Severity

NOW = datetime(2026, 10, 15, 7, 55, tzinfo=timezone.utc)
LEAKED = "어제 밤에 이미 아이폰 자동화 점검을 요청했으니 오늘은 중복 알림을 보낸다."


def test_말할_때() -> None:
    reply = '{"speak": true, "message": "오늘 일찍 자"}'
    assert parse_decision(reply) == Decision(True, "오늘 일찍 자")


def test_말하지_않을_때() -> None:
    assert parse_decision('{"speak": false}') == Decision(False)


def test_말하지_않을_때는_딸려온_문장을_무시한다() -> None:
    assert parse_decision('{"speak": false, "message": "이건 보내면 안 됨"}') == Decision(False)


def test_코드펜스와_군말은_벗겨낸다() -> None:
    reply = '알겠어.\n```json\n{"speak": true, "message": "물 마셔"}\n```'
    assert parse_decision(reply) == Decision(True, "물 마셔")


def test_SKIP_한_단어는_말하지_않음으로_받는다() -> None:
    assert parse_decision("SKIP") == Decision(False)
    assert parse_decision("  skip ") == Decision(False)


def test_판단을_문장으로_풀어_쓴_답은_판단이_아니다() -> None:
    """사고 재현 — 10월 15일에 이게 폰으로 나갔다."""
    assert parse_decision(LEAKED) is None


def test_말하겠다면서_할_말이_비면_깨진_답이다() -> None:
    assert parse_decision('{"speak": true, "message": "  "}') is None
    assert parse_decision('{"speak": true}') is None


def test_speak_가_참거짓이_아니면_깨진_답이다() -> None:
    assert parse_decision('{"speak": "yes", "message": "안녕"}') is None
    assert parse_decision("") is None


@dataclass
class ScriptedReasoner:
    replies: List[str]
    calls: int = 0
    prompts: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.prompts.append(prompt)
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply


def _insight() -> Insight:
    return Insight(
        trigger="stale_data:location", summary="위치가 끊김", severity=Severity.URGENT, at=NOW
    )


def test_깨진_답은_보내지도_기록하지도_않고_다음에_다시_묻는다() -> None:
    """실패를 판단으로 기록하면 쿨다운이 걸려 하루 동안 다시 안 묻는다."""
    brain = ScriptedReasoner([LEAKED, '{"speak": true, "message": "폰 연결 확인해줘"}'])
    agent = JarvisAgent(reasoner=brain)

    assert asyncio.run(agent.consider(_insight(), NOW)) is None
    assert agent.gate.log.last("stale_data:location") is None

    assert asyncio.run(agent.consider(_insight(), NOW)) == "폰 연결 확인해줘"
    assert brain.calls == 2


def test_말하지_않기로_한_건_기록해서_쿨다운을_쓴다() -> None:
    brain = ScriptedReasoner(['{"speak": false}'])
    agent = JarvisAgent(reasoner=brain)
    assert asyncio.run(agent.consider(_insight(), NOW)) is None
    last = agent.gate.log.last("stale_data:location")
    assert last is not None and last.spoken is False
    assert asyncio.run(agent.consider(_insight(), NOW)) is None
    assert brain.calls == 1


def test_지시문이_형식을_요구한다() -> None:
    from src.brain.prompts import SYSTEM_PROMPT

    assert '{"speak": true' in SYSTEM_PROMPT and '{"speak": false}' in SYSTEM_PROMPT
