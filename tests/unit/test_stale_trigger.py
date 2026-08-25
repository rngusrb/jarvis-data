from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.models import Observation, Severity
from src.triggers.stale import StaleDataTrigger

NOW = datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc)


def _seen(hours_ago: float) -> list[Observation]:
    return [
        Observation(
            source="apple_health",
            kind="sleep_hours",
            value=7.0,
            at=NOW - timedelta(hours=hours_ago),
        )
    ]


def test_최근에_들어왔으면_조용하다() -> None:
    assert StaleDataTrigger(kind="sleep_hours", label="수면").check(_seen(10), NOW) is None


def test_하루를_통째로_건너뛰면_감지한다() -> None:
    insight = StaleDataTrigger(kind="sleep_hours", label="수면").check(_seen(40), NOW)
    assert insight is not None
    assert insight.severity is Severity.NOTABLE


def test_사흘_넘게_끊기면_심각하다() -> None:
    insight = StaleDataTrigger(kind="sleep_hours", label="수면").check(_seen(80), NOW)
    assert insight is not None
    assert insight.severity is Severity.URGENT


def test_한_번도_안_들어온_건_멈춤이_아니다() -> None:
    """설정을 마치기도 전에 잔소리를 듣게 할 이유가 없다."""
    assert StaleDataTrigger(kind="sleep_hours", label="수면").check([], NOW) is None


def test_종류마다_이름이_갈린다() -> None:
    """이름이 겹치면 쿨다운을 공유해서 한쪽이 다른 쪽을 막아버린다."""
    sleep = StaleDataTrigger(kind="sleep_hours", label="수면")
    steps = StaleDataTrigger(kind="step_count", label="걸음수")
    assert sleep.name != steps.name


def test_마지막_기록_시점을_알려준다() -> None:
    insight = StaleDataTrigger(kind="sleep_hours", label="수면").check(_seen(40), NOW)
    assert insight is not None
    assert "40시간째" in insight.summary
