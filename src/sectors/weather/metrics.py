"""날씨 예보 카드 — 발표 하나가 관측치 하나다.

시간별 예보는 `meta["hours"]`(한 시간에 한 줄)에 든다. 값(`value`)은 앞으로 24시간의 가장 높은
강수확률이다 — 숫자 하나로 보일 때 그나마 뜻이 있는 것. 시각(`at`)은 **발표 시각**이다: 예보할
때(미래)로 두면 마지막 수신 시각이 사흘 뒤가 되어 받기가 끊겨도 수집 감시가 사흘 넘게 모른다.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import List, Sequence

from src.core.clock import WEEKDAYS, local
from src.core.metrics import Fold, Metric
from src.core.models import Observation
from src.sectors.weather.kma import RAIN_KINDS, Hour

KIND = "weather_forecast"
# 발표가 3시간마다라 두 번 거르면 끊긴 것이다. 수집 감시(카드)와 신호(이보다 오랜 예보는 안 본다)가
# 같이 쓴다.
STALE_AFTER = timedelta(hours=7)


def hours_of(observation: Observation) -> List[Hour]:
    rows = observation.meta.get("hours") or []
    return [Hour.from_meta(row) for row in rows if isinstance(row, dict) and row.get("at")]


def wet_spans(hours: Sequence[Hour]) -> List[str]:
    """비·눈이 오는 때를 이어진 시간끼리 — "07~10시 비(강수확률 최고 70%)"."""
    groups: List[List[Hour]] = []
    for hour in hours:
        if not hour.wet:
            continue
        if groups and hour.at - groups[-1][-1].at == timedelta(hours=1):
            groups[-1].append(hour)
        else:
            groups.append([hour])
    spans = []
    for group in groups:
        kinds = Counter(RAIN_KINDS[h.pty] for h in group if h.pty in RAIN_KINDS)
        kind = kinds.most_common(1)[0][0] if kinds else "비 올 수도"
        first, last = group[0].at, group[-1].at
        when = f"{first:%H}시" if first == last else f"{first:%H}~{last:%H}시"
        top = max(h.pop or 0 for h in group)
        spans.append(f"{when} {kind}(강수확률 최고 {top}%)")
    return spans


def describe_forecast(observation: Observation, now: datetime) -> str:
    """맥락에 넣을 오늘(남은 시간)·내일 예보 한 줄. 날짜는 요일까지 코드가 적는다."""
    hours = hours_of(observation)
    today = local(now).date()
    parts = []
    for label, day in (("오늘", today), ("내일", today + timedelta(days=1))):
        day_hours = [h for h in hours if h.at.date() == day and h.at + timedelta(hours=1) > now]
        if not day_hours:
            continue
        temps = [h.temp for h in day_hours if h.temp is not None]
        temp = f" {min(temps):g}~{max(temps):g}℃" if temps else ""
        rain = ", ".join(wet_spans(day_hours)) or "비·눈 없음"
        span = f"{day_hours[0].at:%H}~{day_hours[-1].at:%H}시"
        parts.append(f"{label} {day:%m/%d}({WEEKDAYS[day.weekday()]}) {span}{temp}, {rain}")
    head = f"{local(observation.at):%m/%d %H}시 발표"
    return f"{head} — " + (" · ".join(parts) if parts else "오늘·내일 예보 없음")


METRICS = [
    Metric(
        kind=KIND,
        label="집 근처 날씨 예보",
        fold=Fold.RAW,
        collector="기상청 단기예보 — 서버가 받는다(공공데이터포털 키, 집의 5km 격자)",
        stale_after=STALE_AFTER,
        via="서버",
        describe=describe_forecast,
    )
]
