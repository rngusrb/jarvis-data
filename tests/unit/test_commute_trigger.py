"""commute 트리거 — 평일 아침 루틴 이탈."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.core.models import Event
from src.sectors.commute.triggers import LateDepartureTrigger, UnusualBoardingTrigger, _clock

KST = timezone(timedelta(hours=9))
MONDAY = datetime(2026, 9, 7, tzinfo=KST)  # 월요일
HOME, STATION, OTHER = 1, 2, 9


def _day(n: int) -> datetime:
    """n 번째 평일 (주말은 건너뛴다)."""
    day, seen = MONDAY, 0
    while True:
        if day.weekday() < 5:
            if seen == n:
                return day
            seen += 1
        day += timedelta(days=1)


def _morning(n: int, leave: float, board_place: int = STATION) -> List[Event]:
    start = _day(n) + timedelta(hours=leave)
    return [
        Event(source="s", kind="location", at=start, label="leave_home", place_id=HOME),
        Event(
            source="s",
            kind="location",
            at=start + timedelta(minutes=12),
            label="wallet",
            place_id=board_place,
        ),
    ]


def _usual(days: int = 6, leave: float = 7.0) -> List[Event]:
    return [e for n in range(days) for e in _morning(n, leave)]


def _now(n: int, hour: int = 10) -> datetime:
    return _day(n) + timedelta(hours=hour)


def _late(events: List[Event], n: int) -> Optional[str]:
    insight = LateDepartureTrigger().check(events, _now(n))
    return insight.summary if insight else None


def test_평소대로_나서면_조용하다() -> None:
    assert _late(_usual() + _morning(6, 7.1), 6) is None


def test_평소보다_30분_넘게_늦게_나서면_말할_거리다() -> None:
    summary = _late(_usual() + _morning(6, 7.75), 6)
    assert summary is not None
    assert "07:45" in summary and "07:00" in summary and "45분" in summary


def test_오늘_나선_기록이_없으면_판단하지_않는다() -> None:
    # 지난날의 늦은 출발을 다시 꺼내 말하지 않는다.
    assert _late(_usual() + _morning(6, 9.0), 7) is None


def test_평일이_나흘_안_되면_판단하지_않는다() -> None:
    assert _late(_usual(days=3) + _morning(3, 9.0), 3) is None


def test_주말은_평소에_넣지도_오늘로_보지도_않는다() -> None:
    saturday = MONDAY + timedelta(days=5, hours=11)
    weekend = [Event(source="s", kind="location", at=saturday, label="leave_home", place_id=HOME)]
    assert LateDepartureTrigger().check(_usual() + weekend, saturday) is None


def test_시각은_한국_시각으로_본다() -> None:
    # UTC 로 저장된 사건이어도 07:00 KST 는 07:00 이다.
    utc = [
        Event(source="s", kind="location", at=e.at.astimezone(timezone.utc), label=e.label)
        for e in _usual() + _morning(6, 7.75)
    ]
    insight = LateDepartureTrigger().check(utc, _now(6))
    assert insight is not None and "07:45" in insight.summary


def test_평소와_다른_곳에서_타면_말할_거리다() -> None:
    insight = UnusualBoardingTrigger().check(_usual() + _morning(6, 7.0, OTHER), _now(6))

    assert insight is not None
    assert f"장소 #{STATION}" in insight.summary and f"장소 #{OTHER}" in insight.summary
    # 역할 후보가 있으면 이름을 같이 준다 — LLM 이 "장소 #2"를 사람 말로 바꿀 재료다.
    assert "아침에 타는 곳" in insight.summary


def test_평소_타는_곳에서_타면_조용하다() -> None:
    assert UnusualBoardingTrigger().check(_usual() + _morning(6, 7.0), _now(6)) is None


def test_평소_타는_곳이_뚜렷하지_않으면_판단하지_않는다() -> None:
    mixed = [e for n in range(6) for e in _morning(n, 7.0, STATION if n % 2 else 3)]
    assert UnusualBoardingTrigger().check(mixed + _morning(6, 7.0, OTHER), _now(6)) is None


def test_시각_표기() -> None:
    assert _clock(7.5) == "07:30"
    assert _clock(7.999) == "08:00"


def test_지난_일은_다시_꺼내지_않는다() -> None:
    # 늦게 나선 날, 자정까지 6시간마다 같은 얘기가 다시 판단됐다(JAR-017 리뷰).
    events = _usual() + _morning(6, 7.75)
    assert LateDepartureTrigger().check(events, _day(6) + timedelta(hours=8, minutes=45))
    assert LateDepartureTrigger().check(events, _day(6) + timedelta(hours=11)) is None


def test_튀는_하루가_평소를_끌고_가지_않는다() -> None:
    # 재택일 10:30 외출 하루가 섞여도 평소는 07:00 이다. 평균이면 40분 늦음이 안 잡혔다.
    usual = _usual(days=9) + _morning(9, 10.5)
    summary = _late(usual + _morning(10, 7.75), 10)
    assert summary is not None and "45분" in summary


def _boarding_at(n: int, lat: float, place: int) -> List[Event]:
    start = _day(n) + timedelta(hours=7)
    return [
        Event(
            source="s",
            kind="location",
            at=start,
            label="wallet",
            lat=lat,
            lon=127.0,
            place_id=place,
        )
    ]


def test_GPS가_튀어_생긴_다른_장소는_다른_곳이_아니다() -> None:
    usual = [e for n in range(6) for e in _boarding_at(n, 37.5, STATION)]
    jump = _boarding_at(6, 37.5 + 160 / 110_540, OTHER)  # 160m 옆 — 새 장소가 됐지만 같은 정류장
    far = _boarding_at(6, 37.52, OTHER)  # 2km 넘게 — 정말 다른 곳

    assert UnusualBoardingTrigger().check(usual + jump, _now(6)) is None
    assert UnusualBoardingTrigger().check(usual + far, _now(6)) is not None
