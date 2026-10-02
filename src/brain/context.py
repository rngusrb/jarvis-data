"""맥락 조립 — 초개인화의 실체가 여기 있다.

"어젯밤 5시간 잤다"는 사실은 그 자체로 의미가 없다. 평소 몇 시간 자는지,
오늘 일정이 뭔지, 어제 이미 같은 잔소리를 했는지를 알아야 말을 걸지 정할 수 있다.
그 재료를 모아오는 게 ContextProvider고, 자비스를 '똑똑하게' 만드는 건
더 큰 모델이 아니라 여기에 제공자를 하나씩 늘리는 일이다.

## 신호는 **선택**이다

맥락은 "신호에 대한 맥락"이 아니라 **"이 사람에 대한 맥락"**이다. 최근 수면값,
믿음, 수집 현황은 신호가 있든 없든 똑같이 쓸모 있다.

사고 이력: 2026-10-02. `fetch(insight: Insight, ...)` 로 신호를 필수로 두었더니
대화에서 쓸 수가 없어서 **가짜 신호를 만들었다**(`trigger="대화"`,
`severity=INFO`). 그 가짜는 `observations` 가 비어서 추이 제공자가 침묵했고,
자비스는 데이터가 멀쩡히 있는데도 "데이터 값이 없어서 판단할 수 없어"라고
답했다. 타입을 속이면 거짓말이 아래로 전파된다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Protocol, Sequence

from src.core.models import Insight

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextBlock:
    """프롬프트에 붙일 맥락 조각 하나."""

    label: str
    body: str


class ContextProvider(Protocol):
    name: str

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        """줄 만한 맥락이 없으면 None. 이게 정상이고 흔한 경우다.

        `insight` 가 None 이면 **사용자가 먼저 꺼낸 말**이다. 신호에 기대는
        제공자는 그때 None 을 돌려주면 되고, 사람 상태를 보는 제공자는
        평소대로 일한다.
        """
        ...


def assemble(
    providers: Sequence[ContextProvider], insight: Optional[Insight], now: datetime
) -> List[ContextBlock]:
    blocks: List[ContextBlock] = []
    for provider in providers:
        try:
            block = provider.fetch(insight, now)
        except Exception:
            # 제공자 하나가 죽어도 자비스는 말은 해야 한다. 맥락이 조금 얕아질 뿐.
            # 캘린더 서버가 내려갔다고 건강 알림까지 멈추면 안 된다.
            logger.exception("맥락 제공자 실패: %s", provider.name)
            continue
        if block is not None:
            blocks.append(block)
    return blocks


def render(blocks: Sequence[ContextBlock]) -> str:
    return "\n\n".join(f"[{block.label}]\n{block.body}" for block in blocks)
