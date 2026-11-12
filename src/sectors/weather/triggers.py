"""weather 섹터의 트리거 — 비·눈이 오는 날을 미리.

저녁(20~22시)엔 내일 아침(06~10시 — 나서는 때), 아침(06~09시)엔 오늘 낮(07~21시)을 본다. 신호의
`at` 은 **그날 0시**다 — 같은 날은 게이트가 한 번만 판단한다(저녁에 말했으면 다음 날 아침엔 다시 안
묻고, 저녁엔 비 예보가 없다가 아침에 생겼으면 그때 한 번).

기준이 절대값(강수형태·강수확률)인 건 이 섹터에선 맞다. 날씨는 사람의 baseline 이 아니라 바깥의
사실이다(triggers/_GUIDE 의 "절대 기준 금지"는 몸·습관의 값 얘기다).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional, Sequence, Tuple

from src.core.clock import KST, WEEKDAYS, local
from src.core.models import Insight, Observation, Severity
from src.sectors.weather.metrics import KIND, STALE_AFTER, hours_of, wet_spans

FAMILY = "weather"
EVENING = range(20, 22)
MORNING = range(6, 9)
COMMUTE_HOURS = range(6, 11)  # 내일 아침 — 06~10시
DAY_HOURS = range(7, 22)  # 오늘 낮 — 07~21시


@dataclass
class RainAheadTrigger:
    name: str = "rain_ahead"
    kind: str = KIND
    lookback: timedelta = timedelta(hours=12)

    def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]:
        if not window:
            return None
        latest = max(window, key=lambda o: o.at)
        if now - latest.at > STALE_AFTER:  # 받기가 끊긴 건 수집 감시가 말한다
            return None
        planned = _target(now)
        if planned is None:
            return None
        day, hours, label = planned
        # 지난 시간은 근거로 안 쓴다 — 08:40 에 05시 발표만 있으면 "07시 비"가 근거가 될
        # 뻔했다(리뷰).
        wet = [
            h
            for h in hours_of(latest)
            if h.at.date() == day
            and h.at.hour in hours
            and h.wet
            and h.at + timedelta(hours=1) > now
        ]
        if not wet:
            return None
        spans = ", ".join(wet_spans(wet))
        summary = (
            f"{label} {day:%m/%d}({WEEKDAYS[day.weekday()]}) 비·눈 예보: {spans} — "
            f"기상청 {local(latest.at):%m/%d %H}시 발표, 집 근처"
        )
        return Insight(
            trigger=self.name,
            summary=summary,
            severity=Severity.NOTABLE,
            at=datetime.combine(day, time(0), tzinfo=KST),
            observations=(latest,),
            family=FAMILY,
        )


def _target(now: datetime) -> Optional[Tuple[date, range, str]]:
    """지금 볼 날과 시간대. 저녁엔 내일 아침, 아침엔 오늘 낮, 그 밖엔 안 본다."""
    when = local(now)
    if when.hour in EVENING:
        return when.date() + timedelta(days=1), COMMUTE_HOURS, "내일 아침(06~10시)"
    if when.hour in MORNING:
        return when.date(), DAY_HOURS, "오늘 낮(07~21시)"
    return None


TRIGGERS = [RainAheadTrigger()]
