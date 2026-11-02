"""장소 — 좌표가 있는 사건을 가까운 것끼리 묶은 것.

도메인 지식이 없는 계산만 둔다: 거리, 가장 가까운 장소, 중심 옮기기. "이 장소가 집인가"
같은 판단은 사건의 이유(`label`)와 시간대를 아는 섹터가 한다.

장소 번호는 바뀌지 않아야 한다. 나중에 사람이나 LLM 이 "집"이라고 이름을 붙이면 그 번호에
붙는다 — 매번 처음부터 다시 묶으면 번호가 바뀌어 이름이 엉뚱한 곳으로 간다. 그래서 새 점을
기존 장소에 하나씩 붙이고(가장 가까운 곳, 반경 안), 없으면 새 장소를 만든다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Tuple

# 이 안쪽이면 같은 장소로 본다. 도시에서 건물 하나 남짓. 2026-10 실측: 위치 74건이 16곳으로
# 묶이고, 큰 곳들의 퍼짐(중심에서 가장 먼 점)이 55~69m 였다.
SAME_PLACE_METERS = 150.0


@dataclass
class Place:
    id: int
    lat: float
    lon: float
    visits: int
    first_seen: datetime
    last_seen: datetime
    # 사람이나 LLM 이 붙인 이름. 붙기 전엔 비어 있다.
    name: str = ""


def meters_between(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """두 좌표 사이 거리(m). 도시 규모에서는 평면 근사로 충분하다."""
    lat_mid = math.radians((a[0] + b[0]) / 2)
    dx = (b[1] - a[1]) * 111_320 * math.cos(lat_mid)
    dy = (b[0] - a[0]) * 110_540
    return math.hypot(dx, dy)


def nearest(
    places: Iterable[Place], lat: float, lon: float, radius: float = SAME_PLACE_METERS
) -> Optional[Place]:
    """반경 안에서 가장 가까운 장소. 여럿이 반경 안이면 가까운 쪽 — 처음 만든 쪽이 아니다."""
    best: Optional[Tuple[float, Place]] = None
    for place in places:
        d = meters_between((place.lat, place.lon), (lat, lon))
        if d <= radius and (best is None or d < best[0]):
            best = (d, place)
    return best[1] if best else None


def visit(place: Place, lat: float, lon: float, at: datetime) -> None:
    """점 하나를 장소에 더한다. 중심은 지금까지 붙은 점들의 평균으로 옮긴다."""
    place.visits += 1
    place.lat += (lat - place.lat) / place.visits
    place.lon += (lon - place.lon) / place.visits
    place.first_seen = min(place.first_seen, at)
    place.last_seen = max(place.last_seen, at)
