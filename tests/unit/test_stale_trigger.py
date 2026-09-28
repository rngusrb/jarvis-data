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


def test_오래된_중단에도_계속_말한다() -> None:
    """사고 재현 — 2026-08-29 수집이 멈추고 한 달간 아무 말도 없었다.

    자비스는 9월 12일까지 매일 경고했다. 창이 14일이라 8월 29일 기록이
    창 밖으로 밀려난 9월 13일부터 침묵했다 — 빈 창이 "아직 시작 안 함"으로
    판정됐기 때문이다. **중단이 심해질수록 감시견이 조용해졌다.**
    """
    last = datetime(2026, 8, 29, 10, 24, tzinfo=timezone.utc)
    trigger = StaleDataTrigger(kind="location", label="위치")
    window = [Observation(source="shortcuts", kind="location", value=0.0, at=last, meta={})]

    # 침묵하지 않는 것이 이 테스트의 요점이다. 9월 13일에 딱 침묵했다.
    for days in (2, 14, 15, 30, 90, 365):
        now = last + timedelta(days=days)
        insight = trigger.check(window, now)
        assert insight is not None, f"{days}일이 지났는데 침묵한다"

    # 그리고 오래될수록 조용해지지 않는다.
    assert trigger.check(window, last + timedelta(days=90)).severity is Severity.URGENT


def test_한_번도_안_들어온_것은_잔소리하지_않는다() -> None:
    """설정을 마치기도 전에 잔소리를 듣게 할 이유가 없다."""
    trigger = StaleDataTrigger(kind="location", label="위치")
    assert trigger.check([], datetime(2026, 9, 28, tzinfo=timezone.utc)) is None
