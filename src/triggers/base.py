"""변화 감지 계약.

트리거는 순수 함수여야 한다 — DB도 LLM도 네트워크도 건드리지 않고,
관측치 목록만 받아서 Insight를 낼지 말지 정한다. 그래야 테스트가 싸고,
"왜 자비스가 이 말을 했는가"를 나중에 재현할 수 있다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional, Protocol, Sequence

from src.core.models import Insight, Observation


class Trigger(Protocol):
    name: str
    kind: str
    lookback: timedelta

    def check(self, window: Sequence[Observation]) -> Optional[Insight]:
        """``window``(= 최근 lookback 구간의 kind 관측치)를 보고 판단한다.

        말할 거리가 없으면 None. 이게 정상이고 대부분의 호출은 None이다.
        """
        ...
