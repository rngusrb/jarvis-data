"""맥 앱 사용 하루 요약.

맥 앞에 뜬 앱은 하루 수백 줄이다 — 앱을 바꿀 때마다, 정각마다, 5분 손을 놓을 때마다 구간이
끊긴다. 낱줄로는 회고에도 맥락에도 못 넣는다(일주일이면 수천 줄이 관심사가 아니라 잡음이 된다).
그래서 하루 한 줄로 접어 보여준다. **저장하지 않는다** — 볼 때마다 흔적에서 다시 접는다.

한 줄에 담는 것: 날짜, 처음~끝 시각, 모두 쓴 시간, 시간대(오전·오후·저녁·새벽), 앱 상위 다섯과
그 외. 창 제목은 수집기가 처음부터 안 읽는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from src.core.clock import KST, local
from src.core.models import Trace

logger = logging.getLogger(__name__)

# 하루는 새벽 5시에 바뀐다. 서버의 93일치(웹 방문 8,593건)에서 05시가 가장 조용했고(52건),
# 앱 사용은 4~7시가 0분이었다. 자정으로 끊으면 새벽 두 시까지 한 일이 다음 날 일처럼 읽힌다.
# 수면의 밤(정오 경계)과는 다른 것이다 — 밤은 잠을, 하루는 깨어 있는 시간을 한 덩어리로 묶는다.
DAY_STARTS = time(5)
# 이름으로 적는 앱 수. 나머지는 "그 외 n개"로 묶는다.
TOP_APPS = 5
# 시간대. 하루가 시작하는 순서대로. 수집기가 구간을 정각마다 끊어서 시작 시각으로 나누면 정확하다.
PARTS: Tuple[Tuple[str, int, int], ...] = (
    ("오전", 5, 12),
    ("오후", 12, 18),
    ("저녁", 18, 24),
    ("새벽", 0, 5),
)
WEEKDAYS = "월화수목금토일"


@dataclass
class _Day:
    first: datetime
    last: datetime
    apps: Dict[str, int] = field(default_factory=dict)
    parts: Dict[str, int] = field(default_factory=dict)

    def add(self, app: str, start: datetime, end: datetime, seconds: int) -> None:
        self.first = min(self.first, start)
        self.last = max(self.last, end)
        self.apps[app] = self.apps.get(app, 0) + seconds
        part = _part(start.hour)
        self.parts[part] = self.parts.get(part, 0) + seconds


def app_days(traces: Sequence[Trace], since: datetime, until: datetime) -> List[str]:
    """`mac_app` 흔적을 하루 한 줄로. 창 앞에서 잘린 날은 빼고, until 이 든 날은 "지금까지"."""
    days: Dict[date, _Day] = {}
    unreadable = 0
    for trace in traces:
        span = _span(trace)
        if span is None:
            unreadable += 1
            continue
        app, start, end, seconds = span
        day = _day_of(start)
        if day not in days:
            days[day] = _Day(first=start, last=end)
        days[day].add(app, start, end, seconds)
    if unreadable:
        # 수집기가 바뀌어 메타 모양이 달라지면 요약이 조용히 줄어든다. 줄 수를 남긴다.
        logger.warning("앱 사용 %d줄은 앱 이름이나 쓴 시간이 없어 하루 요약에서 뺐다", unreadable)

    ongoing = _day_of(local(until))
    lines: List[str] = []
    for day in sorted(days):
        if _day_start(day) < since:
            # 창 앞에서 잘린 날이다. 넣으면 몇 시간만 센 날이 "덜 쓴 날"로 읽힌다.
            continue
        lines.append(_line(day, days[day], ongoing=day == ongoing))
    return lines


def _span(trace: Trace) -> Optional[Tuple[str, datetime, datetime, int]]:
    """(앱, 시작, 끝, 초). 시각은 한국 시간. 메타가 읽히지 않으면 None."""
    app = trace.meta.get("app")
    try:
        seconds = int(trace.meta["seconds"])
    except (KeyError, TypeError, ValueError):
        return None
    if not app or seconds <= 0:
        return None
    start = local(trace.at)
    return str(app), start, start + timedelta(seconds=seconds), seconds


def _day_of(when: datetime) -> date:
    """한국 시각 when 이 어느 하루인가. 새벽 5시 전이면 전날이다."""
    return (when - timedelta(hours=DAY_STARTS.hour)).date()


def _day_start(day: date) -> datetime:
    return datetime.combine(day, DAY_STARTS, tzinfo=KST)


def _part(hour: int) -> str:
    for name, start, end in PARTS:
        if start <= hour < end:
            return name
    raise ValueError(f"시간대에 없는 시각: {hour}")


def _line(day: date, d: _Day, ongoing: bool) -> str:
    end = f"다음 날 {d.last:%H:%M}" if d.last.date() > day else f"{d.last:%H:%M}"
    parts = " · ".join(
        f"{name} {_duration(d.parts[name])}" for name, _, _ in PARTS if name in d.parts
    )
    ranked = sorted(d.apps.items(), key=lambda kv: (-kv[1], kv[0]))
    named = ", ".join(f"{app} {_duration(seconds)}" for app, seconds in ranked[:TOP_APPS])
    rest = ranked[TOP_APPS:]
    if rest:
        named += f", 그 외 {len(rest)}개 {_duration(sum(s for _, s in rest))}"
    head = f"{day:%m-%d}({WEEKDAYS[day.weekday()]}) {d.first:%H:%M}~{end}"
    if ongoing:
        head += " (지금까지)"
    return f"{head}, 모두 {_duration(sum(d.apps.values()))} [{parts}] — {named}"


def _duration(seconds: int) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "1분 미만"
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}시간 {minutes}분"
    return f"{hours}시간" if hours else f"{minutes}분"
