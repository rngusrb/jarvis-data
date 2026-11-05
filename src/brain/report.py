"""보고서 — 자비스가 모은 기록으로 쓰는 "요즘의 너".

반응(`agent.py`)은 신호 하나를 보고 두 문장을, 대화(`converse.py`)는 물은 것에 세 문장을 쓴다. 이건
반대로 **넓게** 본다 — 관심사 나무 전체, 2주치 하루 요약, 석 달 수면·심박, 2주 위치 사건. 사용자가
부를 때(`/report`)만 돈다.

처음엔 한 번 해 보는 실험이었다(2026-11-05, 사용자 요청 "나에 대한 보고서를 써 보게"). 쓸 만했고
틀린 곳이 둘 있었다. 둘 다 기록을 읽는 법을 몰라서였다 — 그래서 지시문에 읽는 법을 적는다.

  하루 요약의 첫 시각(05:12)을 기상 시각으로 읽었다 → "주말엔 05~06시 기상"
  (수면 기록은 10-02 뒤로 없다)
  주제의 "43일"(근거가 있던 날 수)을 연속으로 읽었다 → "자소서 43일째 꾸준"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from src.brain.client import Reasoner
from src.brain.context import ContextProvider, assemble, render
from src.core.clock import shown

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """너는 한 사람의 개인 비서 "자비스"다. 네가 그동안 모은 기록을 보고 그 사람에게
"요즘의 너" 보고서를 쓴다.

기록 읽는 법:
- 하루 요약의 하루는 새벽 5시에 바뀐다. 줄 맨 앞 시각은 그날 처음 기록된 활동이지 일어난
  시각이 아니다. 수면 기록이 없는 날의 잠든·일어난 시각은 모른다고 쓴다.
- 관심사의 "근거가 있던 날"은 날의 수다. 그만큼 연속으로 했다는 뜻이 아니다.
- 사건은 그날 찍힌 순간들이다. 사건이 없는 날은 기록이 없는 것이지 집에 있었다는 뜻이 아니다.

지켜야 할 것:
- 기록에 있는 것만 쓴다. 짐작은 "~로 보여"처럼 짐작이라고 밝히고, 근거(날짜, 날 수, 횟수)를
  같이 쓴다.
- 기록이 끊겼거나 없는 부분은 없다고 솔직하게 쓴다. 지어내지 않는다.
- 훈계하지 않는다. 관찰을 먼저, 제안은 짧게.
- 한국어 반말. 따뜻하지만 담백하게. 텔레그램으로 가니 표는 쓰지 말고 짧은 제목과 목록으로.
- 전체를 3,000자 안으로.

구성:
1. 한 줄 요약
2. 요즘 몰두하는 것 — 관심사를 큰 줄기로 묶어서, 근거와 함께
3. 하루 리듬 — 언제 활동하고, 폰과 맥을 언제 쓰나
4. 잠 — 기록이 있는 기간만
5. 이동 — 출근 루틴과 장소
6. 내가(자비스가) 아직 모르는 것 — 기록이 비는 곳
7. 다음 한 주에 해볼 만한 것 — 두세 개"""

# 모델이 아무것도 못 냈을 때. 빈 답을 그대로 보내면 사용자는 아무 일도 안 일어난 걸로 본다.
FAILED = "보고서를 못 썼어. 조금 뒤에 /report 다시 해 줘."


@dataclass
class Reporter:
    reasoner: Reasoner
    providers: Sequence[ContextProvider]

    async def write(self, now: datetime) -> str:
        # 신호 없이 조립한다 — 보고서는 신호에 대한 것이 아니라 사람에 대한 것이다(brain/_GUIDE).
        blocks = assemble(self.providers, None, now)
        prompt = f"지금: {shown(now)}\n\n{render(blocks)}\n\n위 기록으로 보고서를 써라."
        reply = await self.reasoner.ask(prompt, system=SYSTEM_PROMPT)
        if not reply or not reply.strip():
            logger.warning("보고서 답이 비었다 (재료 %d묶음, %d자)", len(blocks), len(prompt))
            return FAILED
        return reply.strip()
