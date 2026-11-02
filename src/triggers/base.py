"""변화 감지 계약.

트리거는 순수 함수여야 한다 — DB도 LLM도 네트워크도 건드리지 않고,
관측치와 현재 시각만 받아서 Insight를 낼지 말지 정한다. 그래야 테스트가 싸고,
"왜 자비스가 이 말을 했는가"를 나중에 그대로 재현할 수 있다.

``now``를 인자로 받는 이유도 같다. 내부에서 시계를 직접 읽으면 순수성이
깨져서 과거 데이터로 백테스트를 돌릴 수 없게 된다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping, Optional, Protocol, Sequence

from src.core.models import Event, Insight, Observation


class Trigger(Protocol):
    name: str
    kind: str
    lookback: timedelta

    def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]:
        """``window``(= 최근 lookback 구간의 kind 관측치)를 보고 판단한다.

        말할 거리가 없으면 None. 이게 정상이고 대부분의 호출은 None이다.
        """
        ...


class EventTrigger(Protocol):
    """사건(위치 등)의 최근 구간을 보고 판단하는 트리거.

    관측치 트리거와 받는 재료가 다르다. "오늘 평소와 다른 곳에서 탔다"는 숫자의 추이가
    아니라 그날의 사건 순서와 장소로 판단한다. 순수 함수라는 계약은 같다.
    """

    name: str
    kind: str
    lookback: timedelta

    def check(self, events: Sequence[Event], now: datetime) -> Optional[Insight]: ...


class Watch(Protocol):
    """창 없이 "종류별 마지막 수신 시각"만 보고 판단하는 감시.

    트리거와 나눈 이유는 받는 재료가 달라서다. 트리거는 지표 하나의 최근 구간을
    받고, 감시는 모든 종류의 마지막 수신 시각을 한 장으로 받는다. 수집 중단처럼
    "오래될수록 더 중요해지는" 신호는 창에 담으면 오래될수록 안 보인다.
    """

    name: str

    def check(self, last_seen: Mapping[str, datetime], now: datetime) -> Optional[Insight]: ...
