"""장소에 실제 이름을 붙인다 — 카카오로 "아침에 갈아타는 곳" → "가람역 5호선".

서버가 가져오는 일(`src/runtime/pull.py` 의 `Puller`)이다. 역할이 있는 장소(아침에 타는 곳·갈아타는
곳·내리는 곳·저녁에 타는 곳) 중 이름이 아직 없는 곳만 — 사람이 붙인 이름이
이긴다(`SQLitePlaceStore.name`). 반경 안에 지하철역이 있으면 역 이름, 없으면 동네 이름.

**집 장소의 좌표는 보내지 않는다** — 역할 이름이 아니라 장소 번호로 거른다("집"이라는 이름이 이미
있다). 다만 집 가까운 곳의 좌표는 그 장소로 나간다: 아침에 타는 정류장(집에서 수백 m)의 좌표가
카카오에 간다(2026-11-11 실측) — 동네까지는 알 수 있는 거리다.

2026-11-11 실측(운영 기록): 아침에 타는 곳 → 가나구 다라동(역 없음 — 버스 정류장), 갈아타는 곳 →
가람역 5호선(152m), 내리는 곳·저녁에 타는 곳 → 나루역 2호선(48m).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Protocol, Sequence

import httpx

from src.core.places import Place
from src.sectors.commute.kakao import nearest_station, region
from src.sectors.commute.places import HOME, LOCATION_KIND, LOOKBACK, RecentEvents, roles

logger = logging.getLogger(__name__)

# 이 안에 지하철역이 있으면 그 역에서 탄 것으로 본다. 장소 반경(150m)과 GPS 튐을 더한 만큼.
STATION_METERS = 200


class NamingPlaces(Protocol):
    def all(self) -> Sequence[Place]: ...

    def name(self, place_id: int, name: str) -> bool: ...


@dataclass
class PlaceNaming:
    client: httpx.AsyncClient
    key: str
    events: RecentEvents
    places: NamingPlaces
    name: str = "장소 이름"
    every: timedelta = timedelta(hours=6)

    async def pull(self, now: datetime) -> str:
        found = roles(self.events.recent(LOCATION_KIND, now - LOOKBACK))
        by_id = {p.id: p for p in self.places.all()}
        # 집은 역할 이름이 아니라 장소 번호로 거른다 — 집 장소가 "아침에 타는 곳"을 겹쳐 가지면 역할
        # 이름만 보고는 집 좌표가 나갔다(리뷰 JAR-044 재현).
        home = {r.place_id for r in found if r.name == HOME}
        done: List[str] = []
        seen = set()
        for role in found:
            place = by_id.get(role.place_id)
            if place is None or place.id in home or place.name or place.id in seen:
                continue
            seen.add(place.id)
            station = await nearest_station(
                self.client, self.key, place.lat, place.lon, STATION_METERS
            )
            if station is not None:
                label, why = station[0], f"지하철역 {station[1]}m"
            else:
                area = await region(self.client, self.key, place.lat, place.lon)
                if area is None:
                    logger.warning("장소 #%s(%s)의 동네를 카카오가 모른다", place.id, role.name)
                    continue
                label, why = area, "동네(가까운 역 없음)"
            if self.places.name(place.id, label):
                done.append(f"#{place.id} {role.name} → {label}({why})")
        return "; ".join(done) or "새로 붙일 이름 없음"
