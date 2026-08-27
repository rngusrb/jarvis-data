"""만성 수면 부족 트리거.

이 트리거는 CLAUDE.md 의 "절대 기준 금지" 원칙에 대한 의도적 예외라서,
예외를 정당화한 조건이 유지되는지를 테스트가 지킨다 — 즉 (1) baseline
방식이 못 잡는 걸 잡고, (2) 하루치 편차로는 안 울린다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.models import Observation, Severity
from src.sectors.health.triggers import ChronicShortSleepTrigger, SleepDropTrigger

NOW = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)


def night(days_ago: int, hours: float, segments: int = 16) -> Observation:
    return Observation(
        source="apple_health",
        kind="sleep_hours",
        at=NOW - timedelta(days=days_ago),
        value=hours,
        meta={"segments": segments},
    )


def test_catches_what_baseline_misses() -> None:
    """이 트리거가 존재하는 이유 그 자체 — 실제 사고 데이터를 그대로 쓴다.

    2026-08-27 이 사용자는 2.77시간을 잤는데 최근 평균이 3.91시간이라
    SleepDropTrigger 는 침묵했다. 평소가 망가지면 급락이 안 보인다.
    """
    window = [
        night(20, 4.00),
        night(15, 4.35),
        night(14, 3.85),
        night(8, 4.71),
        night(3, 2.24),
        night(2, 2.85),
        night(1, 5.37),
        night(0, 2.77),
    ]

    assert SleepDropTrigger().check(window, NOW) is None

    insight = ChronicShortSleepTrigger().check(window, NOW)
    assert insight is not None
    assert insight.trigger == "chronic_short_sleep"
    assert insight.severity is Severity.URGENT


def test_stays_quiet_when_usually_fine() -> None:
    """하루 못 잔 건 이 트리거의 일이 아니다. 그건 SleepDropTrigger 몫."""
    window = [night(i, 7.5) for i in range(7, 0, -1)] + [night(0, 3.0)]
    assert ChronicShortSleepTrigger().check(window, NOW) is None


def test_notable_below_healthy_urgent_below_four() -> None:
    mild = [night(i, 5.5) for i in range(6, -1, -1)]
    insight = ChronicShortSleepTrigger().check(mild, NOW)
    assert insight is not None and insight.severity is Severity.NOTABLE

    severe = [night(i, 3.5) for i in range(6, -1, -1)]
    insight = ChronicShortSleepTrigger().check(severe, NOW)
    assert insight is not None and insight.severity is Severity.URGENT


def test_needs_enough_records_to_call_it_a_pattern() -> None:
    """기록 네 개로 "요즘 계속"이라고 말할 수는 없다."""
    assert ChronicShortSleepTrigger().check([night(i, 3.0) for i in range(4)], NOW) is None


def test_measurement_failures_do_not_manufacture_a_pattern() -> None:
    """조각 1개짜리 기록만 모아 평균을 내면 없는 만성을 지어낸다."""
    broken = [night(i, 0.3, segments=1) for i in range(8)]
    assert ChronicShortSleepTrigger().check(broken, NOW) is None
