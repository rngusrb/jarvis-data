"""흔적 하루 요약 — 맥 앱 사용, 웹 방문.

맥 앞에 뜬 앱은 하루 수백 줄이다 — 앱을 바꿀 때마다, 정각마다, 5분 손을 놓을 때마다 구간이
끊긴다. 웹 방문도 하루 수십~수백 줄이다. 낱줄로는 자비스의 맥락에 못 넣는다. 그래서 하루 한 줄로
접어 보여준다. **저장하지 않는다** — 볼 때마다 흔적에서 다시 접는다.

하루를 나누고, 창 앞에서 잘린 날을 빼고, 빈 날을 "기록 없음"으로 적는 일은 두 요약이 같이 쓴다
(`_render`). 한 줄에 무엇을 담는지만 다르다.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple, TypeVar

from src.core.clock import KST, WEEKDAYS, local
from src.core.models import Trace

logger = logging.getLogger(__name__)

# 하루는 새벽 5시에 바뀐다. 서버의 93일치(웹 방문 8,593건)에서 05시가 가장 조용했고(52건),
# 앱 사용은 4~7시가 0분이었다. 자정으로 끊으면 새벽 두 시까지 한 일이 다음 날 일처럼 읽힌다.
# 수면의 밤(정오 경계)과는 다른 것이다 — 밤은 잠을, 하루는 깨어 있는 시간을 한 덩어리로 묶는다.
DAY_STARTS = time(5)
# 이름으로 적는 앱 수. 나머지는 "그 외 n개"로 묶는다.
TOP_APPS = 5
# 웹: 많이 간 곳 몇 개, 최근 검색어 몇 개, 검색어 하나의 길이. 오류 메시지를 통째로 붙여 검색하면
# 한 줄이 500자를 넘었다(리뷰 재현) — 맥락은 판단과 대화마다 붙는다.
TOP_HOSTS = 5
LAST_SEARCHES = 5
SEARCH_CHARS = 40
# 크롬 수집기(`collectors/chrome_history.py` 의 describe)가 검색 흔적 앞에 붙이는 말. 둘이 어긋나면
# 검색이 조용히 보통 페이지로 세어진다 — 시험이 둘을 묶는다.
SEARCH = "검색: "
# 시간대. 하루가 시작하는 순서대로. 수집기가 구간을 정각마다 끊어서 시작 시각으로 나누면 정확하다.
# 새벽은 하루가 바뀌는 시각까지다 — 5시를 따로 들고 있으면 한쪽만 바뀌어 순서가 조용히 어긋난다.
PARTS: Tuple[Tuple[str, int, int], ...] = (
    ("오전", DAY_STARTS.hour, 12),
    ("오후", 12, 18),
    ("저녁", 18, 24),
    ("새벽", 0, DAY_STARTS.hour),
)


D = TypeVar("D")


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
    """`mac_app` 흔적을 하루 한 줄로. 날을 나누는 규칙은 `_render`."""
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

    return _render(days, since, until, _line)


@dataclass
class _WebDay:
    first: datetime
    last: datetime
    # 방문 수. 수집기가 같은 30분 칸의 같은 페이지를 한 줄로 접고 몇 번 봤는지 meta.visits 에 남긴다
    # — 관심의 강도라 줄 수가 아니라 방문 수로 센다(리뷰: 스무 번 새로고침한 곳이 1 로 세어졌다).
    visits: int = 0
    devices: Counter = field(default_factory=Counter)
    parts: Counter = field(default_factory=Counter)
    hosts: Counter = field(default_factory=Counter)
    searches: List[str] = field(default_factory=list)


def web_days(traces: Sequence[Trace], since: datetime, until: datetime) -> List[str]:
    """`web_visit` 흔적을 하루 한 줄로 — 몇 곳(어느 기기), 시간대, 많이 간 곳, 최근 검색어.

    크롬은 다른 기기(폰 크롬)의 방문도 동기화해 온다. 폰 데이터가 끊겨도 새벽에 폰으로 본 게 여기
    보인다. 검색어까지 넣는다 — 민감하지만 실험 단계라 자비스가 보게 한다(2026-11-02 사용자 결정).
    """
    days: Dict[date, _WebDay] = {}
    hostless = 0
    for trace in sorted(traces, key=lambda t: t.at):
        when = local(trace.at)
        day = _day_of(when)
        if day not in days:
            days[day] = _WebDay(first=when, last=when)
        d = days[day]
        seen = _visits(trace)
        d.last = max(d.last, when)
        d.visits += seen
        d.devices[str(trace.meta.get("device") or "기기 모름")] += seen
        d.parts[_part(when.hour)] += seen
        if trace.text.startswith(SEARCH):
            term = _short(trace.text[len(SEARCH) :])
            if term and (not d.searches or d.searches[-1] != term):
                d.searches.append(term)
        elif trace.meta.get("host"):
            d.hosts[str(trace.meta["host"])] += seen
        else:
            hostless += 1
    if hostless:
        logger.warning("웹 방문 %d줄은 주소가 없어 많이 간 곳에서 뺐다", hostless)
    return _render(days, since, until, _web_line)


def _visits(trace: Trace) -> int:
    try:
        return max(1, int(trace.meta.get("visits", 1)))
    except (TypeError, ValueError):
        return 1


def _short(term: str) -> str:
    """검색어 한 줄. 공백(줄바꿈 포함)을 하나로 접고 SEARCH_CHARS 자에서 자른다."""
    folded = " ".join(term.split())
    return folded if len(folded) <= SEARCH_CHARS else folded[: SEARCH_CHARS - 1] + "…"


def _render(
    days: Mapping[date, D], since: datetime, until: datetime, line: Callable[[date, D, bool], str]
) -> List[str]:
    """날마다 한 줄. 창 앞에서 잘린 날은 빼고, until 이 든 날은 "지금까지".

    기록이 있는 첫 날부터 지금까지, 흔적 없는 날은 "기록 없음"으로 적는다. 빼 버리면 안 쓴 날인지
    수집이 멈춘 날인지 모른 채 그날이 없었던 것처럼 읽힌다. 첫 기록 앞은 적지 않는다 — 수집기가
    생기기 전 날까지 "기록 없음"이면 "요즘 쓰기 시작했다"로 읽힌다(재리뷰). 보일 날에 기록이 하나도
    없으면 빈 목록이다 — 그건 수집 감시가 말할 일이다.
    """
    # 창 앞에서 잘린 날은 뺀다. 넣으면 몇 시간만 센 날이 "덜 쓴 날"로 읽힌다.
    shown = [d for d in days if _day_start(d) >= since]
    if not shown:
        return []
    ongoing = _day_of(local(until))
    day = min(shown)
    lines: List[str] = []
    while day <= max(ongoing, max(shown)):
        if day in days:
            lines.append(line(day, days[day], day == ongoing))
        else:
            lines.append(f"{_date(day)} 기록 없음" + (" (지금까지)" if day == ongoing else ""))
        day += timedelta(days=1)
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


def _date(day: date) -> str:
    return f"{day:%m-%d}({WEEKDAYS[day.weekday()]})"


def _clock(when: datetime, day: date) -> str:
    """그 하루 안의 시각. 자정을 넘겼으면 "다음 날"을 붙인다 — 자정 뒤에만 쓴 날의 시작 시각이
    빠지면 "월요일 01시"로 읽혀 하루가 어긋난다(JAR-019 리뷰 재현)."""
    return f"다음 날 {when:%H:%M}" if when.date() > day else f"{when:%H:%M}"


def _head(day: date, first: datetime, last: datetime, ongoing: bool) -> str:
    start = _clock(first, day)
    # 시작부터 자정을 넘겼으면 "다음 날"은 앞에 한 번만 붙인다.
    end = f"{last:%H:%M}" if first.date() > day else _clock(last, day)
    return f"{_date(day)} {start}~{end}" + (" (지금까지)" if ongoing else "")


def _line(day: date, d: _Day, ongoing: bool) -> str:
    parts = " · ".join(
        f"{name} {_duration(d.parts[name])}" for name, _, _ in PARTS if name in d.parts
    )
    ranked = sorted(d.apps.items(), key=lambda kv: (-kv[1], kv[0]))
    named = ", ".join(f"{app} {_duration(seconds)}" for app, seconds in ranked[:TOP_APPS])
    rest = ranked[TOP_APPS:]
    if rest:
        named += f", 그 외 {len(rest)}개 {_duration(sum(s for _, s in rest))}"
    head = _head(day, d.first, d.last, ongoing)
    return f"{head}, 모두 {_duration(sum(d.apps.values()))} [{parts}] — {named}"


def _web_line(day: date, d: _WebDay, ongoing: bool) -> str:
    devices = " · ".join(f"{name} {n}" for name, n in d.devices.most_common())
    parts = " · ".join(f"{name} {d.parts[name]}" for name, _, _ in PARTS if d.parts[name])
    line = f"{_head(day, d.first, d.last, ongoing)}, 방문 {d.visits}번({devices}) [{parts}]"
    tails = []
    if d.hosts:
        ranked = sorted(d.hosts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_HOSTS]
        tails.append("많이 간 곳 " + ", ".join(f"{host} {n}" for host, n in ranked))
    if d.searches:
        recent = ", ".join(f"'{term}'" for term in d.searches[-LAST_SEARCHES:])
        tails.append(f"검색 {len(d.searches)}번: {recent}")
    return line + (" — " + " · ".join(tails) if tails else "")


def _duration(seconds: int) -> str:
    # 사사오입. round() 는 짝수 쪽으로 반올림해 90초가 2분, 270초가 4분이 됐다(리뷰).
    minutes = int(seconds / 60 + 0.5)
    if minutes < 1:
        return "1분 미만"
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}시간 {minutes}분"
    return f"{hours}시간" if hours else f"{minutes}분"
