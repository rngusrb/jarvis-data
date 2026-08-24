"""자비스의 에이전트.

추론 스택을 vLLM 직접 호출로 낮춘 대신, "무엇을 근거로 판단할지"는 여기서 짠다.
저쪽(LangGraph)에 맡기지 않는 이유는 판단에 필요한 재료가 전부 이 레포에 있기
때문이다 — 건강 관측치, 발화 기억, 곧 붙을 캘린더와 대화 기록까지.

지금은 1패스(게이트 → 맥락 조립 → 추론)다. 나중에 자비스가 말만 하는 게 아니라
행동까지 하게 되면(캘린더에 회복 시간 잡기 등) 그 루프도 consider() 안에서
자란다. 위쪽(app/loop.py)은 그때도 안 바뀐다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from src.brain.client import Reasoner
from src.brain.context import ContextProvider, assemble
from src.brain.gate import Gate
from src.brain.prompts import SYSTEM_PROMPT, build_prompt, parse_decision
from src.core.models import Insight


@dataclass
class JarvisAgent:
    reasoner: Reasoner
    gate: Gate = field(default_factory=Gate)
    providers: Sequence[ContextProvider] = ()

    async def consider(self, insight: Insight, now: datetime) -> Optional[str]:
        """이 신호에 대해 사용자에게 건넬 말. 입 다물기로 하면 None."""
        if not self.gate.allows(insight, now):
            return None

        blocks = assemble(self.providers, insight, now)
        prompt = build_prompt(insight, blocks)
        reply = await self.reasoner.ask(prompt, system=SYSTEM_PROMPT)
        return parse_decision(reply)

    def confirm_spoken(self, insight: Insight, now: datetime, text: str) -> None:
        """발송에 **성공했을 때만** 부른다.

        실패한 발송까지 기억에 남기면, 사용자는 메시지를 못 받았는데
        자비스는 "아까 말했지" 하고 쿨다운 내내 침묵한다.
        """
        self.gate.log.record(insight.trigger, now, text)
