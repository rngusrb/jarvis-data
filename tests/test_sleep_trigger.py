from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.models import Observation, Severity
from src.triggers.sleep import SleepDropTrigger

BASE = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(days=10)


def _nights(values: list[float]) -> list[Observation]:
    return [
        Observation(source="apple_health", kind="sleep_hours", value=v, at=BASE + timedelta(days=i))
        for i, v in enumerate(values)
    ]


def test_평소대로_자면_아무_말도_안_한다() -> None:
    trigger = SleepDropTrigger()
    assert trigger.check(_nights([7.0, 7.2, 6.9, 7.1, 7.0]), NOW) is None


def test_baseline보다_짧으면_감지한다() -> None:
    trigger = SleepDropTrigger()
    insight = trigger.check(_nights([7.0, 7.2, 6.9, 7.1, 5.0]), NOW)
    assert insight is not None
    assert insight.trigger == "sleep_drop"
    assert insight.severity is Severity.NOTABLE


def test_급감하면_URGENT로_올라간다() -> None:
    trigger = SleepDropTrigger()
    insight = trigger.check(_nights([7.5, 7.5, 7.5, 7.5, 3.0]), NOW)
    assert insight is not None
    assert insight.severity is Severity.URGENT


def test_짧게_자는_사람에게는_같은_시간도_정상이다() -> None:
    """절대 기준이 아니라 개인 baseline을 쓰는지 확인하는 테스트."""
    trigger = SleepDropTrigger()
    # 평소 5시간대로 자는 사람의 5.2시간은 이상 신호가 아니다.
    assert trigger.check(_nights([5.0, 5.3, 5.1, 5.2, 5.2]), NOW) is None


def test_데이터가_부족하면_판단하지_않는다() -> None:
    trigger = SleepDropTrigger()
    assert trigger.check(_nights([7.0, 3.0]), NOW) is None


def _night(value: float, day: int, segments: int) -> Observation:
    return Observation(
        source="apple_health",
        kind="sleep_hours",
        value=value,
        at=BASE + timedelta(days=day),
        meta={"segments": segments},
    )


def test_측정_실패한_밤은_판단하지_않는다() -> None:
    """조각 1개짜리 0.18시간은 잔 게 아니라 워치가 못 잰 것이다."""
    trigger = SleepDropTrigger()
    nights = [_night(v, i, 15) for i, v in enumerate([7.0, 7.2, 6.9, 7.1])]
    nights.append(_night(0.18, 4, segments=1))
    assert trigger.check(nights, NOW) is None


def test_측정_실패는_baseline도_오염시키지_않는다() -> None:
    trigger = SleepDropTrigger()
    nights = [_night(v, i, 15) for i, v in enumerate([7.0, 7.2, 6.9])]
    nights.append(_night(0.18, 3, segments=1))  # 이게 평균에 들어가면 baseline이 무너진다
    nights.append(_night(5.4, 4, 16))

    insight = trigger.check(nights, NOW)
    assert insight is not None
    # 0.18이 섞였다면 평균이 5.3 아래로 내려가 감지되지 않았을 것이다.
    assert "7.0시간" in insight.summary


def test_값은_낮아도_제대로_잰_밤은_판단한다() -> None:
    """통계만 봤다면 2.82시간을 정상으로 통과시켰을 것이다 — 조각 수가 갈라준다."""
    trigger = SleepDropTrigger()
    nights = [_night(v, i, 15) for i, v in enumerate([7.0, 7.2, 6.9, 7.1, 2.82])]
    assert trigger.check(nights, NOW) is not None


def test_품질_정보가_없으면_통과시킨다() -> None:
    """단축어로 들어온 데이터엔 segments가 없다. 모르는 것과 나쁜 것은 다르다."""
    trigger = SleepDropTrigger()
    assert trigger.check(_nights([7.0, 7.2, 6.9, 7.1, 5.0]), NOW) is not None


def test_URGENT는_절반_이하일_때다() -> None:
    trigger = SleepDropTrigger()
    insight = trigger.check(_nights([10.0, 10.0, 10.0, 10.0, 4.9]), NOW)
    assert insight is not None and insight.severity is Severity.URGENT


def test_절반보다_많이_잤으면_NOTABLE이다() -> None:
    """감소량은 5시간이나 되지만 평소의 절반은 넘겼다."""
    trigger = SleepDropTrigger()
    insight = trigger.check(_nights([10.0, 10.0, 10.0, 10.0, 5.1]), NOW)
    assert insight is not None and insight.severity is Severity.NOTABLE
