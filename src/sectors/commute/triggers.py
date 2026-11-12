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
from src.core.days import is_workday
from src.core.models import Event, Insight, Severity
from src.core.places import meters_between
from src.sectors.commute.places import LEAVE_HOME, LOOKBACK, MORNING, WALLET, roles


def _weekday_mornings(events: Iterable[Event]) -> Dict[date, List[Event]]:
    """평일(공휴일 빼고) 아침마다의 사건. 공휴일 아침은 출근이 아니다 — 늦게 나섬·다른 곳에서 탐의
    판단에도, 평소(baseline)에도 안 넣는다(`core/days`)."""
    days: Dict[date, List[Event]] = {}
    for e in sorted(events, key=lambda e: e.at):
        when = local(e.at)
        if is_workday(when.date()) and when.hour in MORNING:
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

# 이 섹터 신호들의 줄기. 늦게 나섬과 평소와 다른 탑승은 같은 아침을 본다 — 나선 때와 탄 때가
# 몇 분 차이로 따로 들어오므로, 한 아침에 두 말이 나가지 않게 하는 건 줄기 간격이다(app/main.py).
FAMILY = "commute"


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
    lookback: timedelta = LOOKBACK
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
            family=FAMILY,
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
    lookback: timedelta = LOOKBACK
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
            family=FAMILY,
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


# 열차 알림의 줄기 — 늦게 나섬·다른 곳에서 탐(이동)과 따로 센다. 같은 아침에 둘 다 쓸모가 있다.
TRAIN_FAMILY = "train"
# 출발이 이보다 오래면 열차 얘기는 늦었다(타는 곳 → 갈아타는 곳 평소 5~8분 — 2026-11-11 실측. 아침
# 탭은 서버에 바로 닿는다 — 받은 때와 일어난 때가 같았다).
TRAIN_FRESH = timedelta(minutes=10)
BOARD, TRANSFER, ALIGHT = "아침에 타는 곳", "아침에 갈아타는 곳", "아침에 내리는 곳"


def _minute_up(at: datetime) -> datetime:
    """분으로 올린다 — 07:12:50 → 07:13, 07:12:00 → 07:12."""
    whole = at.replace(second=0, microsecond=0)
    return whole if whole == at else whole + timedelta(minutes=1)


@dataclass
class TrainAheadTrigger:
    """평일 아침 나서자마자 — 탈 역의 열차 도착을 알려 줄 때(계획형, JAR-044).

    갈아타는 곳이 있으면(버스 → 지하철) 오늘 타는 곳의 첫 탭이 출발이고 갈아타는 곳이 역이다.
    없으면 집을 나선 때가 출발이고 타는 곳이 역이다. 역에 닿는 때는 한 시각이 아니라 범위다 — 최근
    평일의 "출발 → 역" 걸린 시간의 가장 짧은~가장 긴 것. 그곳이 정말 지하철역인지, 어느 방향인지,
    어느 열차를 탈지는 장소 이름·자리·시간표를 아는 준비(`subway.TrainArrivals`)가 본다 — 트리거는
    순수 함수라 장소 표를 못 본다. 그래서 신호 이름에 역의 장소 번호, 방향을 정할 내리는 곳, 역에
    닿는 범위를 싣는다(`train_ahead:<역>:<내리는 곳>:<빠른 HHMM>:<늦은 HHMM>`).
    """

    name: str = "train_ahead"
    kind: str = "location"
    lookback: timedelta = LOOKBACK
    baseline_days: int = 10
    min_days: int = 3
    fresh_for: timedelta = TRAIN_FRESH
    # 볼일이 있던 아침처럼 튀는 날은 범위에서 뺀다 — 걸린 시간의 중앙값에서 이만큼 넘게 벗어난 날.
    # 하루 80분이 섞이자 범위가 07:09~08:23 이 되어 16줄 중 14줄이 "빠듯"이었다(3차 리뷰 재현).
    # "평균 대신 중앙값"을 쓰게 된 늦게 나섬의 사고와 같은 꼴이다.
    outlier: timedelta = timedelta(minutes=10)

    def check(self, events: Sequence[Event], now: datetime) -> Optional[Insight]:
        mornings = _weekday_mornings(events)
        today = local(now).date()
        if today not in mornings:
            return None
        named = {r.name: r.place_id for r in roles(events)}
        board, goal = named.get(BOARD), named.get(ALIGHT)
        if board is None or goal is None:
            return None  # 내리는 곳을 모르면 방향을 못 정한다
        transfer = named.get(TRANSFER)
        station = transfer if transfer is not None else board

        def start_of(morning: List[Event]) -> Optional[Event]:
            if transfer is not None:
                first = next((e for e in morning if e.label == WALLET), None)
                return first if first is not None and first.place_id == board else None
            return next((e for e in morning if e.label == LEAVE_HOME), None)

        start = start_of(mornings[today])
        if start is None or now - start.at > self.fresh_for:
            return None
        rides = []
        for day in sorted(d for d in mornings if d < today)[-self.baseline_days :]:
            began = start_of(mornings[day])
            if began is None:
                continue
            reached = next(
                (
                    e
                    for e in mornings[day]
                    if e.label == WALLET and e.place_id == station and e.at > began.at
                ),
                None,
            )
            if reached is not None:
                rides.append((reached.at - began.at).total_seconds() / 60)
        middle = median(rides) if rides else 0.0
        rides = [r for r in rides if abs(r - middle) <= self.outlier.total_seconds() / 60]
        if len(rides) < self.min_days:
            return None
        # 그날 역에 닿는 때는 한 시각이 아니라 범위다 — 최근 평일의 가장 빠른~가장 늦은 걸린
        # 시간(중앙값 하나로는 "탈 수 있다/없다"를 못 가렸다 — 리뷰 JAR-044, 실제 차이
        # −1.9~+2.5분).
        early = start.at + timedelta(minutes=min(rides))
        late = start.at + timedelta(minutes=max(rides))
        if now >= early:
            return None  # 빨리 닿는 날이면 이미 역이다
        began_as = BOARD if transfer is not None else "집을 나섬"
        reached_as = TRANSFER if transfer is not None else BOARD
        # 신호에는 분까지만 싣는다 — 둘 다 올린다. 내리면 범위가 최대 59초 앞당겨져, 가장 늦은 날엔
        # 놓칠 열차가 "여유"가 되고 가장 빠른 날에도 못 탈 열차가 "빠듯"이 된다(3차 리뷰 재현: 늦은
        # 끝 07:12:50 을 07:12 로 싣자 07:12:20 열차가 "여유"). 올리면 틀려도 덜 약속하는 쪽이다.
        first, last = _minute_up(local(early)), _minute_up(local(late))
        return Insight(
            trigger=f"{self.name}:{station}:{goal}:{first:%H%M}:{last:%H%M}",
            summary=(
                f"평일 아침 — 오늘 {local(start.at):%H:%M} {began_as}에서 출발했다. "
                # 장소 번호는 신호 이름에만 — 요약에 두니 "장소 #5 가람역"이 말로 샜다(15번 중 1).
                f"평소 {min(rides):.0f}~{max(rides):.0f}분 뒤 {reached_as}에 "
                f"닿는다({first:%H:%M}~{last:%H:%M}쯤, 최근 {len(rides)}일). "
                "그 역에서 탈 열차를 알려 줄 때다."
            ),
            severity=Severity.NOTABLE,
            at=start.at,
            family=TRAIN_FAMILY,
        )


# 관측치를 보는 트리거는 지금 없다.
TRIGGERS: list = []
EVENT_TRIGGERS = [LateDepartureTrigger(), UnusualBoardingTrigger(), TrainAheadTrigger()]
