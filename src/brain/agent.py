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
        """이 신호에 대해 사용자에게 건넬 말. 입 다물기로 하면 None.

        None을 돌려주는 경우가 둘인데 **뒤쪽만 기록한다.**

          게이트가 막음   이미 쿨다운 중이다. 기록할 판단이 없다.
          LLM이 SKIP      판단이 끝났다. 기록해야 쿨다운이 소모된다.

        둘을 구별할 수 있는 건 이 함수뿐이라 기록도 여기서 한다. 밖에서
        None만 보고 기록하면 게이트가 막을 때마다 새 기록이 쌓이고,
        **쿨다운 시계가 계속 앞으로 밀려 영영 말을 못 하게 된다**
        (사고 이력: 2026-09-30, SKIP 52건이 0~9분 간격으로 쌓였다).
        """
        if not self.gate.allows(insight, now):
            return None

        blocks = assemble(self.providers, insight, now)
        prompt = build_prompt(insight, blocks)
        reply = await self.reasoner.ask(prompt, system=SYSTEM_PROMPT)
        message = parse_decision(reply)
        if message is None:
            self.confirm_silence(insight, now, "말 안 걸기로 함")
        return message

    def confirm_spoken(self, insight: Insight, now: datetime, text: str) -> None:
        """발송에 **성공했을 때만** 부른다.

        실패한 발송까지 기억에 남기면, 사용자는 메시지를 못 받았는데
        자비스는 "아까 말했지" 하고 쿨다운 내내 침묵한다.
        """
        self.gate.log.record(insight.trigger, now, text, spoken=True)

    def confirm_silence(self, insight: Insight, now: datetime, reason: str) -> None:
        """**말 안 걸기로 판정했을 때** 부른다.

        발송 실패와 갈라놓는 게 핵심이다. 발송 실패는 다시 시도해야 하지만,
        이건 판단이 끝난 것이다.

        사고 이력: 2026-09-29. 20시간에 LLM 68번 호출 / 발화 2번. 게이트가
        쿨다운을 넘겨 통과시키면 LLM이 SKIP을 내놓는데 그게 아무 데도 안
        남아서 30분 뒤 똑같이 물어봤다. "싼 게이트 먼저, 비싼 LLM 나중"이
        여기서 새고 있었다.
        """
        self.gate.log.record(insight.trigger, now, reason, spoken=False)
