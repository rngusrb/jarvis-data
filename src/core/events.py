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
from typing import Any, Dict, Iterable, List, Mapping, Optional

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

    @property
    def active(self) -> bool:
        return self.collector is not None


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
    source: str, kind: str, at: datetime, value: float, meta: Mapping[str, Any]
) -> Event:
    """점 하나(시각·값·meta)를 사건으로. 수신구와 옮기기(app/migrate.py)가 같이 쓴다.

    단축어는 위치를 표본 모양 — `value` 와 `meta.lat/lon/trigger` — 으로 보낸다.
    폰을 고치지 않으려고 모양은 그대로 받고 여기서 갈라 담는다. 값은 GPS 정확도(m)일
    때만 남긴다. 단축어는 0 을 보내는데, 0m 정확도는 없는 값이다.
    """
    rest = dict(meta)
    lat, lon = rest.pop("lat", None), rest.pop("lon", None)
    label = str(rest.pop("trigger", None) or "")
    if value:
        rest["accuracy"] = value
    return Event(
        source=source,
        kind=kind,
        at=at,
        label=label,
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
        meta=rest,
    )
