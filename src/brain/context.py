"""맥락 조립 — 초개인화의 실체가 여기 있다.

"어젯밤 5시간 잤다"는 사실은 그 자체로 의미가 없다. 평소 몇 시간 자는지,
오늘 일정이 뭔지, 어제 이미 같은 잔소리를 했는지를 알아야 말을 걸지 정할 수 있다.
그 재료를 모아오는 게 ContextProvider고, 자비스를 '똑똑하게' 만드는 건
더 큰 모델이 아니라 여기에 제공자를 하나씩 늘리는 일이다.
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

    def fetch(self, insight: Insight, now: datetime) -> Optional[ContextBlock]:
        """줄 만한 맥락이 없으면 None. 이게 정상이고 흔한 경우다."""
        ...


def assemble(
    providers: Sequence[ContextProvider], insight: Insight, now: datetime
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
