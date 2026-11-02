"""commute 섹터의 트리거 — 평일 아침 루틴에서 벗어났는지.

둘 다 위치 **사건**을 본다. 기준은 절대 시각("8시 넘으면 늦음")이 아니라 본인의 최근 평일들
이다 — 6시에 나서는 사람과 9시에 나서는 사람에게 같은 기준을 대면 한쪽은 매일 울리고 다른
쪽은 영영 안 울린다.

2026-10 까지는 관측치(`work_departure`)를 보는 트리거가 있었는데 그 지표를 보내는 수집기가
없어 30분마다 빈 창을 읽었다(JAR-014 에서 등록을 껐다). 평일 baseline 판단은 그대로 옮겨왔다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence

from src.core.clock import local
from src.core.models import Event, Insight, Severity
from src.core.places import meters_between
from src.sectors.commute.places import MORNING, WALLET, roles


def _weekday_mornings(events: Iterable[Event]) -> Dict[date, List[Event]]:
    days: Dict[date, List[Event]] = {}
    for e in sorted(events, key=lambda e: e.at):
        when = local(e.at)
        if when.weekday() < 5 and when.hour in MORNING:
            days.setdefault(when.date(), []).append(e)
    return days


def morning_starts(events: Iterable[Event]) -> Dict[date, Event]:
    """평일마다 아침 첫 위치 사건 — 집을 나선 무렵.

    이유는 가리지 않는다. "집 나섬"이 안 찍힌 날은 첫 지갑 탭이 대신하는데, 그만큼(10분 남짓)
    늦게 잡힌다. 판단 문턱(30분)이 그보다 커서 둔다.
    """
    return {day: taps[0] for day, taps in _weekday_mornings(events).items()}


def first_boardings(events: Iterable[Event]) -> Dict[date, Event]:
    """평일마다 아침 첫 지갑 탭."""
    found: Dict[date, Event] = {}
    for day, morning in _weekday_mornings(events).items():
        taps = [e for e in morning if e.label == WALLET]
        if taps:
            found[day] = taps[0]
    return found


def _hours(event: Event) -> float:
    when = local(event.at)
    return when.hour + when.minute / 60


def _clock(hour: float) -> str:
    """7.5 → '07:30'."""
    h, m = int(hour), round((hour - int(hour)) * 60)
    if m == 60:
        h, m = h + 1, 0
    return f"{h:02d}:{m:02d}"


def _past(by_day: Dict[date, Event], today: date, keep: int) -> List[Event]:
    return [by_day[d] for d in sorted(by_day) if d < today][-keep:]


# 서버가 받은 지 이만큼 지난 사건은 다시 판단하지 않는다. 오늘 시작이 있으면 자정까지 매 주기
# 같은 신호가 나가 6시간 쿨다운마다 LLM 이 다시 불렸다 — 늦게 나선 날 같은 얘기가 세 통
# (2026-11-02 JAR-017 리뷰). 수집 직후 주기가 바로 판단하므로 세 시간이면 넉넉하다.
# **받은 때**로 잰다. 일어난 때로 재면 맥을 거쳐 늦게 들어온 아침이 아무 기록 없이 빠졌다(재리뷰).
FRESH_FOR = timedelta(hours=3)


def _just_arrived(event: Event, now: datetime, fresh_for: timedelta) -> bool:
    return now - (event.received_at or event.at) <= fresh_for


# 평소 타는 곳에서 이보다 가까우면 같은 곳이다. 단축어가 정확도를 안 보내 튄 점을 못 거르는데,
# 150m 넘게 튀면 새 장소가 되어 "다른 곳에서 탔다"가 나간다(리뷰 합성). 다른 역은 보통 훨씬 멀다.
SAME_STOP_METERS = 300.0


@dataclass
class LateDepartureTrigger:
    """평일 아침, 평소보다 늦게 나섰는지."""

    name: str = "late_departure"
    kind: str = "location"
    # 폰 데이터가 띄엄띄엄 와서(52일 중 9일) 평일 기록이 모이려면 창이 넓어야 한다.
    lookback: timedelta = timedelta(days=42)
    baseline_days: int = 10
    # 나흘은 모여야 "평소"다. 그보다 적으면 그 평소가 우연이다.
    min_days: int = 4
    late_hours: float = 0.5
    fresh_for: timedelta = FRESH_FOR

    def check(self, events: Sequence[Event], now: datetime) -> Optional[Insight]:
        starts = morning_starts(events)
        today = local(now).date()
        if today not in starts or not _just_arrived(starts[today], now, self.fresh_for):
            # 오늘 나선 기록이 방금 들어왔을 때만 판단한다. 판단한 일을 다시 꺼내지 않는다.
            return None
        past = _past(starts, today, self.baseline_days)
        if len(past) < self.min_days:
            return None
        # 평균이 아니라 중앙값이다. 재택일 10:30 외출 하루가 평균을 끌어올리면 다음 날 진짜
        # 40분 늦음이 안 잡혔다(리뷰 재현).
        usual = median(_hours(e) for e in past)
        left = _hours(starts[today])
        if left - usual < self.late_hours:
            return None
        return Insight(
            trigger=self.name,
            summary=(
                f"오늘 아침 {_clock(left)}에 나섰다. 최근 평일 {len(past)}일 평소"
                f"({_clock(usual)})보다 {round((left - usual) * 60)}분 늦다."
            ),
            severity=Severity.NOTABLE,
            at=starts[today].at,
        )


@dataclass
class UnusualBoardingTrigger:
    """평일 아침 첫 탑승이 평소 타는 곳이 아닌지."""

    name: str = "unusual_boarding"
    kind: str = "location"
    lookback: timedelta = timedelta(days=42)
    baseline_days: int = 10
    min_days: int = 4
    # 최근 평일의 이만큼은 같은 곳에서 타야 "평소 타는 곳"이 있다고 본다.
    min_share: float = 0.6
    fresh_for: timedelta = FRESH_FOR

    def check(self, events: Sequence[Event], now: datetime) -> Optional[Insight]:
        boards = first_boardings(events)
        today = local(now).date()
        if today not in boards or not _just_arrived(boards[today], now, self.fresh_for):
            return None
        past = _past(boards, today, self.baseline_days)
        if len(past) < self.min_days:
            return None
        usual, days = Counter(e.place_id for e in past).most_common(1)[0]
        here = boards[today].place_id
        if usual is None or here is None or here == usual or days / len(past) < self.min_share:
            return None
        if _close_to(boards[today], [e for e in past if e.place_id == usual]):
            return None
        named = {r.place_id: r.name for r in roles(events)}
        return Insight(
            trigger=self.name,
            summary=(
                f"오늘 아침 첫 탑승이 평소 타는 곳(장소 #{usual}{_as(named.get(usual))}, "
                f"최근 평일 {len(past)}일 중 {days}일)이 아니라 "
                f"장소 #{here}{_as(named.get(here))}였다."
            ),
            severity=Severity.NOTABLE,
            at=boards[today].at,
        )


def _close_to(event: Event, usual: Sequence[Event]) -> bool:
    """평소 타는 곳의 점들 평균에서 SAME_STOP_METERS 안이면 같은 곳으로 본다."""
    points = [(e.lat, e.lon) for e in usual if e.lat is not None and e.lon is not None]
    if event.lat is None or event.lon is None or not points:
        return False
    center = (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
    return meters_between(center, (event.lat, event.lon)) < SAME_STOP_METERS


def _as(role: Optional[str]) -> str:
    return f" — {role}" if role else ""


# 관측치를 보는 트리거는 지금 없다.
TRIGGERS: list = []
EVENT_TRIGGERS = [LateDepartureTrigger(), UnusualBoardingTrigger()]
