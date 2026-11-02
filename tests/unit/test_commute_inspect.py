"""위치 점검 도구의 거름망 — 좌표, 정확도, 자동화별로 나누기."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.models import Event
from src.sectors.commute.inspect import accurate, by_trigger, coords

KST = timezone(timedelta(hours=9))
BASE = datetime(2026, 8, 24, 9, 0, tzinfo=KST)
CITY_HALL = (37.5663, 126.9779)
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


def test_좌표가_없으면_건너뛴다() -> None:
    """좌표 없이 들어온 사건이 섞여도 죽지 않는다."""
    assert coords(Event(source="s", kind="location", at=BASE)) is None
    assert coords(_at(*CITY_HALL, 9)) == CITY_HALL


def test_정확도가_나쁜_점은_패턴에서_뺀다() -> None:
    assert accurate(_at(*CITY_HALL, 9, accuracy=20.0))
    assert not accurate(_at(*CITY_HALL, 9, accuracy=500.0))
    # 단축어는 정확도를 안 보낸다. 모르는 것은 뺄 이유가 없다.
    assert accurate(Event(source="s", kind="location", at=BASE, lat=1.0, lon=1.0))


def test_자동화별로_나눈다() -> None:
    """집을 나선 지점과 카드를 찍은 지점은 전혀 다른 사실이다."""
    groups = by_trigger([_at(*CITY_HALL, 7), _at(*GANGNAM, 8, label="tmoney")])
    assert set(groups) == {"(라벨 없음)", "tmoney"}
    assert len(groups["tmoney"]) == 1


def test_라벨이_없어도_죽지_않는다() -> None:
    assert list(by_trigger([_at(*CITY_HALL, 9)])) == ["(라벨 없음)"]
