"""장소 계산 — 거리, 가장 가까운 곳, 중심 옮기기."""

from __future__ import annotations

from datetime import datetime, timezone

from src.core.places import Place, meters_between, nearest, visit

AT = datetime(2026, 10, 2, tzinfo=timezone.utc)
# 서울시청 근처 두 점과, 걸어서 갈 수 없는 거리의 한 점
CITY_HALL = (37.5663, 126.9779)
NEXT_DOOR = (37.5670, 126.9782)  # 약 80m
GANGNAM = (37.4979, 127.0276)


def _place(id: int, lat: float, lon: float) -> Place:
    return Place(id, lat, lon, 1, AT, AT)


def test_거리를_잰다() -> None:
    assert meters_between(CITY_HALL, NEXT_DOOR) < 150
    assert meters_between(CITY_HALL, GANGNAM) > 5000


def test_반경_안에서_가장_가까운_곳을_고른다() -> None:
    # 처음 만든 곳이 아니라 가까운 곳이다.
    far_in_radius = _place(1, 37.5675, 126.9779)  # 약 130m
    close = _place(2, *NEXT_DOOR)
    assert nearest([far_in_radius, close], *CITY_HALL) is close
    assert nearest([_place(3, *GANGNAM)], *CITY_HALL) is None


def test_중심은_붙은_점들의_평균으로_옮긴다() -> None:
    place = _place(1, 37.0, 127.0)
    visit(place, 37.002, 127.002, AT.replace(day=3))

    assert (round(place.lat, 6), round(place.lon, 6)) == (37.001, 127.001)
    assert place.visits == 2 and place.last_seen.day == 3
