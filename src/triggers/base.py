"""변화 감지 계약.

트리거는 순수 함수여야 한다 — DB도 LLM도 네트워크도 건드리지 않고,
관측치와 현재 시각만 받아서 Insight를 낼지 말지 정한다. 그래야 테스트가 싸고,
"왜 자비스가 이 말을 했는가"를 나중에 그대로 재현할 수 있다.

``now``를 인자로 받는 이유도 같다. 내부에서 시계를 직접 읽으면 순수성이
깨져서 과거 데이터로 백테스트를 돌릴 수 없게 된다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Protocol, Sequence

from src.core.models import Insight, Observation


class Trigger(Protocol):
    name: str
    kind: str
    lookback: timedelta

    def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]:
        """``window``(= 최근 lookback 구간의 kind 관측치)를 보고 판단한다.

        말할 거리가 없으면 None. 이게 정상이고 대부분의 호출은 None이다.
        """
        ...
