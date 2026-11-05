"""보고서 — 자비스가 모은 기록으로 쓰는 "요즘의 너".

반응(`agent.py`)은 신호 하나를 보고 두 문장을, 대화(`converse.py`)는 물은 것에 세 문장을 쓴다. 이건
반대로 **넓게** 본다 — 관심사 나무 전체, 2주치 하루 요약, 석 달 수면·심박, 2주 위치 사건. 사용자가
부를 때(`/report`)만 돈다.

처음엔 한 번 해 보는 실험이었다(2026-11-05, 사용자 요청 "나에 대한 보고서를 써 보게"). 쓸 만했고
틀린 곳이 둘 있었다. 둘 다 기록을 읽는 법을 몰라서였다 — 그래서 읽는 법을 같이 준다.

  하루 요약의 첫 시각(05:12)을 기상 시각으로 읽었다 → "주말엔 05~06시 기상"
  (수면 기록은 10-02 뒤로 없다)
  주제의 "43일"(근거가 있던 날 수)을 연속으로 읽었다 → "자소서 43일째 꾸준"

일반 규칙("근거가 있던 날"은 날의 수)은 지시문에 둔다. 섹터의 사실(하루는 새벽 5시에
바뀐다)은 그 기록 묶음이 들고 온다(`TraceKind.reading`) — 플랫폼 지시문이 섹터를 알면 안
된다(리뷰).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from src.brain.client import Reasoner, unreachable
from src.brain.context import ContextProvider, assemble, render
from src.core.clock import shown

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """너는 한 사람의 개인 비서 "자비스"다. 네가 그동안 모은 기록을 보고 그 사람에게
"요즘의 너" 보고서를 쓴다.

기록 읽는 법:
- 기록 묶음에 "읽는 법"이 붙어 있으면 그대로 따른다(하루가 언제 바뀌는지, 맨 앞 시각이 무엇인지).
- 관심사의 "근거가 있던 날"은 날의 수다. 그만큼 연속으로 했다는 뜻이 아니다.
- "기록 없음"인 날은 기록이 없는 것이다. 그날 아무것도 안 했다거나 집에 있었다는 뜻이 아니다.
- 잠든·일어난 시각처럼 기록에 없는 것은 모른다고 쓴다. 다른 기록에서 미루어 짓지 않는다.

지켜야 할 것:
- 기록에 있는 것만 쓴다. 짐작은 "~로 보여"처럼 짐작이라고 밝히고, 근거(날짜, 날 수, 횟수)를
  같이 쓴다.
- 기록이 끊겼거나 없는 부분은 없다고 솔직하게 쓴다. 지어내지 않는다.
- 훈계하지 않는다. 관찰을 먼저, 제안은 짧게.
- 한국어 반말 문장으로 쓴다("~했어", "~로 보여"). "~함", "~음"으로 끝내는 개조식은 쓰지 않는다.
  따뜻하지만 담백하게.
- 텔레그램은 마크다운을 그리지 않는다. **, #, 표 없이 쓴다 — 제목은 숫자를 붙인 한 줄,
  목록은 "·" 로.
- 전체를 3,000자 안으로.

구성 — 인사나 머리말 없이 1부터 쓴다:
1. 한 줄 요약
2. 요즘 몰두하는 것 — 관심사를 큰 줄기로 묶어서, 근거와 함께
3. 생활 — 나머지 기록 묶음마다 보이는 것을, 기록이 있는 것만
4. 내가(자비스가) 아직 모르는 것 — 기록이 비는 곳
5. 다음 한 주에 해볼 만한 것 — 두세 개"""

# 모델이 아무것도 못 냈을 때. 빈 답을 그대로 보내면 사용자는 아무 일도 안 일어난 걸로 본다.
FAILED = "보고서를 못 썼어. 조금 뒤에 /report 다시 해 줘."
# 두뇌(vLLM)에 연결부터 안 될 때(데스크탑 꺼짐, 길 끊김). 예외를 그대로 올리면 듣기 루프가
# 로그만 남기고 아무 답도 안 보낸다 — 사용자는 "1분쯤 걸려"를 보고 기다리다 끝난다(JAR-027
# 리뷰 재현).
UNREACHABLE = "보고서를 못 썼어 — 두뇌(서버의 모델)에 닿지 않아. 데스크탑이 켜져 있는지 봐 줘."
# 닿았는데 답을 못 받았을 때 — 거절(재료가 모델 한도를 넘음, 서버 오류)이나 읽다가 한도가
# 찼다. 데스크탑은 켜져 있으니 켜져 있나 보라고 하면 틀린 말이다(재리뷰 재현 — vLLM 400 에
# 위 문구가 갔다).
REFUSED = (
    "보고서를 못 썼어 — 두뇌가 답을 못 냈어(이유는 자비스 로그에). 조금 뒤에 /report 다시 해 줘."
)
# 이보다 크면 경고를 남긴다. 모델 한도는 65,536 토큰이고 답 몫이 6,000 이라, 실측 0.62 토큰/자로
# 약 9만 자가 끝이다. 2026-11-05 실측 재료는 1.2만 자.
PROMPT_WARN = 60_000


@dataclass
class Reporter:
    reasoner: Reasoner
    providers: Sequence[ContextProvider]

    async def write(self, now: datetime) -> str:
        """보고서. 실패해도 늘 사용자에게 보낼 말을 돌려준다(이유는 로그에)."""
        # 신호 없이 조립한다 — 보고서는 신호에 대한 것이 아니라 사람에 대한 것이다(brain/_GUIDE).
        blocks = assemble(self.providers, None, now)
        prompt = f"지금: {shown(now)}\n\n{render(blocks)}\n\n위 기록으로 보고서를 써라."
        if len(prompt) > PROMPT_WARN:
            logger.warning("보고서 재료가 크다 — %d자(경고 %d자)", len(prompt), PROMPT_WARN)
        try:
            reply = await self.reasoner.ask(prompt, system=SYSTEM_PROMPT)
        except Exception as error:
            logger.exception(
                "보고서 — 두뇌가 답을 못 냈다 (재료 %d묶음, %d자)", len(blocks), len(prompt)
            )
            return UNREACHABLE if unreachable(error) else REFUSED
        if not reply or not reply.strip():
            logger.warning("보고서 답이 비었다 (재료 %d묶음, %d자)", len(blocks), len(prompt))
            return FAILED
        return reply.strip()
