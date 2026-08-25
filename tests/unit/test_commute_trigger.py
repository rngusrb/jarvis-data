from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.models import Observation, Severity
from src.sectors.commute.triggers import LateDepartureTrigger, _clock

KST = timezone(timedelta(hours=9))
# 2026-08-03 은 월요일
MONDAY = datetime(2026, 8, 3, tzinfo=KST)
NOW = MONDAY + timedelta(days=30)


def _days(values: list[float], start: datetime = MONDAY) -> list[Observation]:
    """평일만 골라 하루씩 채운다."""
    out, day = [], start
    for v in values:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append(Observation(source="shortcuts", kind="work_departure", value=v, at=day))
        day += timedelta(days=1)
    return out


def test_평소대로_퇴근하면_조용하다() -> None:
    trigger = LateDepartureTrigger()
    assert trigger.check(_days([17.5, 17.4, 17.6, 17.5, 17.5]), NOW) is None


def test_평소보다_늦으면_감지한다() -> None:
    trigger = LateDepartureTrigger()
    insight = trigger.check(_days([17.5, 17.4, 17.6, 17.5, 19.5]), NOW)
    assert insight is not None
    assert insight.severity is Severity.NOTABLE
    assert "19:30" in insight.summary


def test_아주_늦으면_URGENT() -> None:
    trigger = LateDepartureTrigger()
    insight = trigger.check(_days([17.5, 17.4, 17.6, 17.5, 21.0]), NOW)
    assert insight is not None and insight.severity is Severity.URGENT


def test_늦게_퇴근하는_사람에게는_같은_시각도_정상이다() -> None:
    """절대 기준이 아니라 본인 패턴을 쓰는지 확인한다."""
    trigger = LateDepartureTrigger()
    assert trigger.check(_days([21.0, 20.8, 21.2, 21.0, 21.1]), NOW) is None


def test_기록이_적으면_판단하지_않는다() -> None:
    """며칠 치로 '평소'를 정하면 그 평소가 우연이다."""
    trigger = LateDepartureTrigger()
    assert trigger.check(_days([17.5, 22.0]), NOW) is None


def test_주말은_baseline에서_뺀다() -> None:
    """주말 기록이 섞이면 주 5일 패턴의 평균이 흐트러진다."""
    trigger = LateDepartureTrigger()
    weekend = Observation(
        source="shortcuts",
        kind="work_departure",
        value=13.0,
        at=datetime(2026, 8, 8, tzinfo=KST),  # 토요일
    )
    window = _days([17.5, 17.4, 17.6, 17.5, 19.5]) + [weekend]
    insight = trigger.check(window, NOW)
    assert insight is not None
    assert "17:30" in insight.summary  # 13시가 섞였다면 평균이 내려갔을 것


def test_시각_표기() -> None:
    assert _clock(17.5) == "17:30"
    assert _clock(9.0) == "09:00"
    assert _clock(17.99) == "17:59"
    # 분이 60으로 반올림되면 시간으로 올린다
    assert _clock(17.999) == "18:00"
