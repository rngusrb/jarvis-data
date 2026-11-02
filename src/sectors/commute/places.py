"""장소의 역할 후보 — 집, 아침에 타는 곳, 아침에 내리는 곳, 저녁에 타는 곳.

장소를 묶는 건 플랫폼(`src/core/places.py`)이고, "그 장소가 무엇인가"는 사건의 이유와
시각을 아는 이 섹터가 정한다. 이건 **이름 후보**다 — 근거 수와 함께 내고, 사람이나 LLM 이
붙인 이름이 있으면 그쪽이 이긴다.

규칙(한국 시각, 평일만):
  집              아침에 "집 나섬"(leave_home)이 가장 많이 찍힌 곳
  아침에 타는 곳   그날 아침 첫 지갑 탭이 가장 자주 찍힌 곳
  아침에 갈아타는 곳 그날 아침 첫 탭과 마지막 탭 사이에 찍힌 곳
  아침에 내리는 곳 그날 아침 마지막 지갑 탭(첫 탭과 다른 곳)이 가장 자주 찍힌 곳
  저녁에 타는 곳   그날 저녁 첫 지갑 탭이 가장 자주 찍힌 곳

2026-10 실측(74건, 16곳): 07시·18시 지갑 탭이 몰린 곳(25회), 08시·17시 지갑 탭이 몰린 곳(12회),
07시 "집 나섬"이 몰린 곳(8회)이 뚜렷이 갈렸다. 가장 많이 찍힌 곳은 타는 곳이 아니라 갈아타는
곳이었다 — 아침 첫 탭은 그 전 정류장에서 찍혔다. 그래서 "가장 많이 찍힌 곳"이 아니라 "그날의
몇 번째 탭인가"로 나눈다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence

from src.core.clock import local
from src.core.models import Event

LEAVE_HOME = "leave_home"
WALLET = "wallet"

MORNING = range(5, 11)  # 05:00~10:59
EVENING = range(16, 22)  # 16:00~21:59

# 이보다 적으면 우연이다. 사흘은 같아야 "평소"라 부른다.
MIN_DAYS = 3


@dataclass(frozen=True)
class Role:
    name: str
    place_id: int
    # 근거가 된 날 수와, 그 규칙을 따질 수 있었던 날 수.
    days: int
    of: int


def roles(events: Iterable[Event]) -> List[Role]:
    weekday = [e for e in events if e.place_id is not None and local(e.at).weekday() < 5]
    found: List[Role] = []

    # 근거는 탭 수가 아니라 **날 수**다. 지오펜스를 들락날락하면 "집 나섬"이 한 아침에 두 번,
    # 버스→지하철 환승역은 내리고 타며 두 번 찍힌다 — 탭으로 세면 이틀이 "나흘 근거"가
    # 되어 사흘 문턱을 넘었다(2026-11-02 JAR-016 리뷰).
    home_days: Dict[date, set] = {}
    for e in weekday:
        if e.label == LEAVE_HOME and local(e.at).hour in MORNING:
            home_days.setdefault(local(e.at).date(), set()).add(e.place_id)
    home = Counter(place for places in home_days.values() for place in places)
    found += _winner("집", home, len(home_days))

    mornings = _taps_by_day(weekday, MORNING)
    first = Counter(taps[0].place_id for taps in mornings.values())
    found += _winner("아침에 타는 곳", first, len(mornings))

    middle = Counter(
        place
        for taps in mornings.values()
        for place in {e.place_id for e in taps[1:-1]} - {taps[0].place_id, taps[-1].place_id}
    )
    found += _winner("아침에 갈아타는 곳", middle, len(mornings))

    last = Counter(
        taps[-1].place_id for taps in mornings.values() if taps[-1].place_id != taps[0].place_id
    )
    found += _winner("아침에 내리는 곳", last, len(mornings))

    evenings = _taps_by_day(weekday, EVENING)
    found += _winner(
        "저녁에 타는 곳", Counter(taps[0].place_id for taps in evenings.values()), len(evenings)
    )
    return found


def _taps_by_day(events: Sequence[Event], hours: range) -> Dict[date, List[Event]]:
    days: Dict[date, List[Event]] = {}
    for e in sorted(events, key=lambda e: e.at):
        when = local(e.at)
        if e.label == WALLET and when.hour in hours:
            days.setdefault(when.date(), []).append(e)
    return days


def _winner(name: str, counts: "Counter[Optional[int]]", of: int) -> List[Role]:
    if not counts:
        return []
    place_id, days = counts.most_common(1)[0]
    if place_id is None or days < MIN_DAYS:
        return []
    return [Role(name, place_id, days, of)]
