"""사건 카드.

흔적 카드(`traces.py`)와 같은 역할을 사건 쪽에서 한다 — 어떤 종류의 사건이 있는지,
누가 보내는지, 언제 끊긴 것으로 볼지를 **한 곳에** 적는다. 카드가 없는 종류는
수신구가 받지 않는다.

사건은 "어느 순간 어디서 무슨 일"이다. 관측치처럼 하루로 접지 않고(하루 평균 위치는
의미가 없다), 흔적처럼 글로 읽지도 않는다. 장소·루틴 같은 패턴은 사건을 묶어 만든다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from src.core.models import Event


@dataclass(frozen=True)
class EventKind:
    kind: str
    label: str
    # 누가 보내나. None 이면 접힌 종류 — 지표·흔적 카드와 같은 규칙이다.
    collector: Optional[str] = None
    stale_after: timedelta = timedelta(days=3)
    # 어느 기기를 거쳐 오나. 수집 감시가 기기별로 묶는 데 쓴다.
    via: Optional[str] = None
    # 표본 모양으로 올 때 value 칸이 뜻하는 것(meta 에 이 이름으로 남긴다). 플랫폼이
    # "value 는 GPS 정확도"를 박아두면 두 번째 사건 종류의 값이 조용히 정확도가 된다.
    value_as: Optional[str] = None
    # 사건 이름(label)이 사람 말로 무엇인가 — ("leave_home", "집을 나섬"). 맥락과 보고서가 쓴다.
    # 섹터가 단다. 플랫폼이 "wallet 은 교통카드 탭"을 알면 섹터를 아는 것이다.
    labels: Tuple[Tuple[str, str], ...] = ()

    @property
    def active(self) -> bool:
        return self.collector is not None

    def describe(self, label: str) -> str:
        """사건 이름의 뜻. 카드가 모르는 이름은 그대로 둔다.

        조용히 빼면 그런 사건이 없었던 것처럼 보인다.
        """
        return dict(self.labels).get(label, label)


class EventSource(Protocol):
    """사건을 읽어오는 곳. 사건 트리거가 이걸로 창을 받는다."""

    def recent(self, kind: str, since: datetime) -> Sequence[Event]: ...


class EventRegistry:
    """섹터들이 자기 사건 종류를 등록하는 곳."""

    def __init__(self) -> None:
        self._cards: Dict[str, EventKind] = {}

    def register(self, kinds: Iterable[EventKind]) -> "EventRegistry":
        for card in kinds:
            if card.kind in self._cards:
                raise ValueError(f"사건 종류 '{card.kind}'가 이미 등록되어 있다")
            self._cards[card.kind] = card
        return self

    def get(self, kind: str) -> Optional[EventKind]:
        return self._cards.get(kind)

    def all(self) -> List[EventKind]:
        return sorted(self._cards.values(), key=lambda c: c.kind)

    def active(self) -> List[EventKind]:
        return [c for c in self.all() if c.active]


def event_from_point(
    source: str,
    kind: str,
    at: datetime,
    value: float,
    meta: Mapping[str, Any],
    value_as: Optional[str] = None,
) -> Event:
    """점 하나(시각·값·meta)를 사건으로. 수신구와 옮기기(app/migrate.py)가 같이 쓴다.

    표본 모양 — `value` 와 `meta.lat/lon/trigger` — 으로 온 것을 갈라 담는다. 폰 단축어를
    고치지 않으려고 모양은 그대로 받는다. 값은 0 이 아닐 때만 남긴다(단축어는 0 을 보내는데,
    "없음"이다). 남길 이름은 카드가 정한다(`value_as`).

    좌표가 숫자로 읽히지 않으면 ValueError 다. 수신구는 422 로 돌려보낸다 — 500 이면 맥
    배달부가 "나중에 다시"로 보고 그 뒤의 모든 줄을 막는다(2026-11-02 JAR-015 리뷰 재현).
    """
    rest = dict(meta)
    lat, lon = _coordinate(rest.pop("lat", None), "lat"), _coordinate(rest.pop("lon", None), "lon")
    label = str(rest.pop("trigger", None) or "")
    if value:
        rest[value_as or "value"] = value
    return Event(source=source, kind=kind, at=at, label=label, lat=lat, lon=lon, meta=rest)


def _coordinate(raw: Any, name: str) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"좌표 {name} 가 숫자가 아니다: {raw!r}") from None
