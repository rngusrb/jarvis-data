from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from src.core.models import Observation, Severity
from src.sectors.health.triggers import SleepDropTrigger

# 밤 관측치의 시각은 깬 날의 한국 자정이다. 수면 트리거는 오늘 아침 끝난 밤만 판단하므로(JAR-018)
# 판단 시각은 다섯째 밤(넷째 날)이 끝난 아침 9시다.
BASE = datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=9)))
NOW = BASE + timedelta(days=4, hours=9)


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


def test_오늘_아침_끝난_밤이_아니면_급감도_말하지_않는다() -> None:
    # 새 밤이 안 들어오면 지난 밤을 쿨다운마다 다시 "어젯밤"으로 물었다(JAR-017 리뷰 재현).
    trigger = SleepDropTrigger()
    nights = _nights([7.5, 7.5, 7.5, 7.5, 3.0])
    assert trigger.check(nights, NOW + timedelta(days=2)) is None


def test_넘긴_급감한_밤은_한_번만_남긴다(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    trigger = SleepDropTrigger()
    nights = _nights([7.5, 7.5, 7.5, 7.5, 3.0])
    next_day = NOW + timedelta(days=1)

    assert trigger.check(nights, next_day) is None
    assert trigger.check(nights, next_day + timedelta(minutes=30)) is None
    assert caplog.text.count("오늘 밤이 아니라 넘긴다") == 1


def test_이탈_없는_지난_밤은_남기지_않는다(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    nights = _nights([7.0, 7.2, 6.9, 7.1, 7.0])
    assert SleepDropTrigger().check(nights, NOW + timedelta(days=1)) is None
    assert "넘긴다" not in caplog.text


def test_UTC_로_들어온_아침도_오늘_아침이다() -> None:
    # 운영 루프는 지금 시각을 UTC 로 넘긴다. 한국 07:30 은 UTC 로 전날 22:30 이라, 날짜를 한국
    # 시간으로 바꾸지 않으면 밤이 보통 들어오는 00~09시에 오늘 밤을 넘긴다(JAR-018 리뷰 돌연변이).
    nights = _nights([7.5, 7.5, 7.5, 7.5, 3.0])
    morning = (NOW - timedelta(hours=1, minutes=30)).astimezone(timezone.utc)
    assert morning.date() != NOW.date()
    assert SleepDropTrigger().check(nights, morning) is not None


def test_URGENT는_절반_이하일_때다() -> None:
    trigger = SleepDropTrigger()
    insight = trigger.check(_nights([10.0, 10.0, 10.0, 10.0, 4.9]), NOW)
    assert insight is not None and insight.severity is Severity.URGENT


def test_절반보다_많이_잤으면_NOTABLE이다() -> None:
    """감소량은 5시간이나 되지만 평소의 절반은 넘겼다."""
    trigger = SleepDropTrigger()
    insight = trigger.check(_nights([10.0, 10.0, 10.0, 10.0, 5.1]), NOW)
    assert insight is not None and insight.severity is Severity.NOTABLE


def _onset_night(day: int, onset: str, hours: float = 7.0, segments: int = 12) -> Observation:
    """day 번째 밤. onset 은 한국 시각 "HH:MM" — 18시 전이면 자정을 넘긴 시각이다."""
    kst = timezone(timedelta(hours=9))
    night = datetime(2026, 9, 1, tzinfo=kst) + timedelta(days=day)
    h, m = map(int, onset.split(":"))
    # 정오 이후 시각은 전날(밤이 시작된 날)의 시각이다 — 수면 집계의 경계와 같다.
    slept = night + timedelta(hours=h, minutes=m) - (timedelta(days=1) if h >= 12 else timedelta(0))
    return Observation(
        source="apple_health",
        kind="sleep_hours",
        value=hours,
        at=night,
        meta={"segments": segments, "onset": slept.isoformat()},
    )


def _late_sleep(nights: list) -> Optional[str]:
    from src.sectors.health.triggers import LateSleepTrigger

    now = nights[-1].at + timedelta(hours=10)
    insight = LateSleepTrigger().check(nights, now)
    return insight.summary if insight else None


def test_평소처럼_잠들면_조용하다() -> None:
    usual = [_onset_night(d, "01:50") for d in range(7)]
    assert _late_sleep(usual + [_onset_night(7, "02:30")]) is None


def test_평소보다_유난히_늦게_잠들면_말할_거리다() -> None:
    usual = [_onset_night(d, "01:50") for d in range(7)]
    summary = _late_sleep(usual + [_onset_night(7, "04:20")])
    assert summary is not None and "04:20" in summary and "01:50" in summary


def test_자정을_넘어도_이어서_잰다() -> None:
    # 평소 23:30, 어젯밤 02:00 — 시계 숫자로는 작아졌지만 2.5시간 늦다.
    usual = [_onset_night(d, "23:30") for d in range(7)]
    summary = _late_sleep(usual + [_onset_night(7, "02:00")])
    assert summary is not None and "2.5시간" in summary


def test_측정이_실패한_밤은_기준에도_판단에도_넣지_않는다() -> None:
    usual = [_onset_night(d, "01:50") for d in range(7)]
    broken = _onset_night(7, "06:00", segments=1)
    assert _late_sleep(usual + [broken]) is None


def test_기록이_넷이_안_되면_판단하지_않는다() -> None:
    assert (
        _late_sleep([_onset_night(0, "01:50"), _onset_night(1, "01:50"), _onset_night(2, "05:00")])
        is None
    )


def test_오늘_아침_끝난_밤이_아니면_말하지_않는다() -> None:
    # 새 밤이 안 들어오면 지난 밤을 며칠씩 "어젯밤"으로 다시 말했다(JAR-017 리뷰).
    from src.sectors.health.triggers import LateSleepTrigger

    nights = [_onset_night(d, "01:50") for d in range(7)] + [_onset_night(7, "04:20")]
    later = nights[-1].at + timedelta(days=2, hours=10)
    assert LateSleepTrigger().check(nights, later) is None


def test_저녁에_일찍_잠든_밤은_늦게_잠든_게_아니다() -> None:
    # 17:00 에 시작한 잠은 다음 밤에 배정된다. 저녁 6시로 재면 "15시간 늦음"이었다.
    usual = [_onset_night(d, "01:50") for d in range(7)]
    assert _late_sleep(usual + [_onset_night(7, "17:00")]) is None
    # 그 밤이 기준에 섞여도 다음 날 진짜 늦은 밤을 놓치지 않는다(중앙값).
    mixed = usual[:6] + [_onset_night(6, "17:00")]
    assert _late_sleep(mixed + [_onset_night(7, "04:20")]) is not None


def test_잠든_시각이_깨져_있으면_그_밤만_뺀다() -> None:
    usual = [_onset_night(d, "01:50") for d in range(7)]
    broken = Observation(
        source="apple_health",
        kind="sleep_hours",
        value=7.0,
        at=usual[3].at,
        meta={"segments": 12, "onset": "01:30"},
    )
    summary = _late_sleep(usual[:3] + [broken] + usual[4:] + [_onset_night(7, "04:20")])
    assert summary is not None


def test_넘긴_늦은_밤은_한_번만_남긴다(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from src.sectors.health.triggers import LateSleepTrigger

    caplog.set_level(logging.INFO)
    nights = [_onset_night(d, "01:50") for d in range(7)] + [_onset_night(7, "04:20")]
    trigger = LateSleepTrigger()
    next_day = nights[-1].at + timedelta(days=1, hours=1)

    assert trigger.check(nights, next_day) is None
    assert trigger.check(nights, next_day + timedelta(minutes=30)) is None
    assert caplog.text.count("오늘 밤이 아니라 넘긴다") == 1
