from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.models import Event
from src.sectors.commute.inspect import cluster, coords, meters_between

KST = timezone(timedelta(hours=9))
BASE = datetime(2026, 8, 24, 9, 0, tzinfo=KST)

# 서울시청 근처 두 점과, 걸어서 갈 수 없는 거리의 한 점
CITY_HALL = (37.5663, 126.9779)
NEXT_DOOR = (37.5670, 126.9782)  # 약 80m
GANGNAM = (37.4979, 127.0276)


def _at(lat: float, lon: float, hour: int, accuracy: float = 20.0, label: str = "") -> Event:
    return Event(
        source="shortcuts",
        kind="location",
        at=BASE.replace(hour=hour),
        label=label,
        lat=lat,
        lon=lon,
        meta={"accuracy": accuracy},
    )


def test_거리를_잰다() -> None:
    assert meters_between(CITY_HALL, NEXT_DOOR) < 150
    assert meters_between(CITY_HALL, GANGNAM) > 5000


def test_가까운_점끼리_묶인다() -> None:
    groups = cluster([_at(*CITY_HALL, 9), _at(*NEXT_DOOR, 18), _at(*GANGNAM, 12)])
    assert len(groups) == 2
    assert len(groups[0]) == 2  # 큰 군집이 먼저


def test_좌표가_없으면_건너뛴다() -> None:
    """좌표 없이 들어온 사건이 섞여도 죽지 않는다."""
    naked = Event(source="s", kind="location", at=BASE)
    assert coords(naked) is None
    assert cluster([naked, _at(*CITY_HALL, 9)]) == [[_at(*CITY_HALL, 9)]]


def test_빈_입력() -> None:
    assert cluster([]) == []


def test_자동화별로_나눈다() -> None:
    """집을 나선 지점과 카드를 찍은 지점은 전혀 다른 사실이다."""
    from src.sectors.commute.inspect import by_trigger

    leave = _at(*CITY_HALL, 7)
    tap = _at(*GANGNAM, 8, label="tmoney")
    groups = by_trigger([leave, tap])
    assert set(groups) == {"(라벨 없음)", "tmoney"}
    assert len(groups["tmoney"]) == 1


def test_라벨이_없어도_죽지_않는다() -> None:
    from src.sectors.commute.inspect import by_trigger

    groups = by_trigger([_at(*CITY_HALL, 9)])
    assert list(groups) == ["(라벨 없음)"]


def test_정확도가_나쁜_점은_패턴에서_뺀다() -> None:
    from src.sectors.commute.inspect import accurate

    assert accurate(_at(*CITY_HALL, 9, accuracy=20.0))
    assert not accurate(_at(*CITY_HALL, 9, accuracy=500.0))
    # 단축어는 정확도를 안 보낸다. 모르는 것은 뺄 이유가 없다.
    assert accurate(Event(source="s", kind="location", at=BASE, lat=1.0, lon=1.0))
