from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.models import Observation
from src.sectors.commute.inspect import cluster, coords, meters_between

KST = timezone(timedelta(hours=9))
BASE = datetime(2026, 8, 24, 9, 0, tzinfo=KST)

# 서울시청 근처 두 점과, 걸어서 갈 수 없는 거리의 한 점
CITY_HALL = (37.5663, 126.9779)
NEXT_DOOR = (37.5670, 126.9782)  # 약 80m
GANGNAM = (37.4979, 127.0276)


def _at(lat: float, lon: float, hour: int, accuracy: float = 20.0) -> Observation:
    return Observation(
        source="shortcuts",
        kind="location",
        value=accuracy,
        at=BASE.replace(hour=hour),
        meta={"lat": lat, "lon": lon},
    )


def test_거리를_잰다() -> None:
    assert meters_between(CITY_HALL, NEXT_DOOR) < 150
    assert meters_between(CITY_HALL, GANGNAM) > 5000


def test_가까운_점끼리_묶인다() -> None:
    groups = cluster([_at(*CITY_HALL, 9), _at(*NEXT_DOOR, 18), _at(*GANGNAM, 12)])
    assert len(groups) == 2
    assert len(groups[0]) == 2  # 큰 군집이 먼저


def test_좌표가_없으면_건너뛴다() -> None:
    """meta 없이 들어온 관측치가 섞여도 죽지 않는다."""
    naked = Observation(source="s", kind="location", value=10.0, at=BASE)
    assert coords(naked) is None
    assert cluster([naked, _at(*CITY_HALL, 9)]) == [[_at(*CITY_HALL, 9)]]


def test_빈_입력() -> None:
    assert cluster([]) == []
