from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.models import Observation, Severity
from src.triggers.sleep import SleepDropTrigger

BASE = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


def _nights(values: list[float]) -> list[Observation]:
    return [
        Observation(source="apple_health", kind="sleep_hours", value=v, at=BASE + timedelta(days=i))
        for i, v in enumerate(values)
    ]


def test_평소대로_자면_아무_말도_안_한다() -> None:
    trigger = SleepDropTrigger()
    assert trigger.check(_nights([7.0, 7.2, 6.9, 7.1, 7.0])) is None


def test_baseline보다_짧으면_감지한다() -> None:
    trigger = SleepDropTrigger()
    insight = trigger.check(_nights([7.0, 7.2, 6.9, 7.1, 5.0]))
    assert insight is not None
    assert insight.trigger == "sleep_drop"
    assert insight.severity is Severity.NOTABLE


def test_급감하면_URGENT로_올라간다() -> None:
    trigger = SleepDropTrigger()
    insight = trigger.check(_nights([7.5, 7.5, 7.5, 7.5, 3.0]))
    assert insight is not None
    assert insight.severity is Severity.URGENT


def test_짧게_자는_사람에게는_같은_시간도_정상이다() -> None:
    """절대 기준이 아니라 개인 baseline을 쓰는지 확인하는 테스트."""
    trigger = SleepDropTrigger()
    # 평소 5시간대로 자는 사람의 5.2시간은 이상 신호가 아니다.
    assert trigger.check(_nights([5.0, 5.3, 5.1, 5.2, 5.2])) is None


def test_데이터가_부족하면_판단하지_않는다() -> None:
    trigger = SleepDropTrigger()
    assert trigger.check(_nights([7.0, 3.0])) is None
