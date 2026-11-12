"""서울 실시간 지하철 도착 — 역 이름 맞추기, 받기, 보여 주기, 그리고 열차 알림
준비(`TrainArrivals`).

이 API 의 역 이름은 카카오 이름과 다르다 — 부제가 붙은 역은 "가람(새터)",
"솔밭(공원앞)"로 물어야 나온다("가람"으로 물으면 "해당하는 데이터가 없습니다" — 2026-11-11
실측). 그래서 먼저 그대로 묻고, 없으면 모든 역의 도착(`ALL`)에서 같은 이름으로 시작하는 정식 이름을
찾아 기억한다. 키는 주소 경로에 들어간다 — 로그에서 지운다(`app/main.py` 의 `hide_in_logs`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

import httpx

from src.brain.context import ContextBlock
from src.core.clock import KST, local
from src.core.models import Insight
from src.core.places import Place, meters_between
from src.sectors.commute.kakao import station_spot
from src.sectors.commute.timetable import (
    Planned,
    Scheduled,
    lateness,
    plan,
    station_code,
    timetable,
)

logger = logging.getLogger(__name__)

Spot = Tuple[float, float]

ARRIVALS = (
    "http://swopenAPI.seoul.go.kr/api/subway/{key}/json/realtimeStationArrival/0/{rows}/{name}"
)
# 노선 이름 → 이 API 의 노선 번호(subwayId).
LINE_IDS = {
    "1호선": "1001",
    "2호선": "1002",
    "3호선": "1003",
    "4호선": "1004",
    "5호선": "1005",
    "6호선": "1006",
    "7호선": "1007",
    "8호선": "1008",
    "9호선": "1009",
    "경의중앙선": "1063",
    "공항철도": "1065",
    "경춘선": "1067",
    "수인분당선": "1075",
    "신분당선": "1077",
    "경강선": "1081",
    "우이신설선": "1092",
    "서해선": "1093",
    "신림선": "1094",
}
# 카카오의 역 이름 — "가람역 5호선", "나루역 2호선".
_STATION = re.compile(r"^(?P<base>.+?)역\s+(?P<line>\S+)$")


@dataclass(frozen=True)
class Station:
    base: str  # "가람"
    line: str  # "5호선"

    @property
    def line_id(self) -> Optional[str]:
        return LINE_IDS.get(self.line)


def parse_station(name: str) -> Optional[Station]:
    """카카오가 붙인 장소 이름이 지하철역이면 역과 노선. 동네 이름("가나구 다라동")이면 None."""
    found = _STATION.match(name.strip())
    if found is None or found.group("line") not in LINE_IDS:
        return None
    return Station(found.group("base"), found.group("line"))


@dataclass(frozen=True)
class Arrival:
    line_id: str
    updown: str  # 상행·하행, 2호선은 내선·외선
    heading: str  # "동끝행 - 동녘방면"
    station: str  # API 의 역 이름 — "가람(새터)"
    seconds: Optional[int]  # 도착까지 남은 초(모르면 None)
    message: str  # "4분 후 (느티)", "전역 도착" — 괄호 속은 행선지가 아니라 **지금 있는 곳**
    at: Optional[datetime]  # 이 정보를 만든 때(한국 시각)
    current: str = ""  # 지금 있는 역(arvlMsg3)
    train_no: str = ""  # 열차번호(btrainNo) — 시간표의 TRAIN_NO 와 같다

    @property
    def arrives(self) -> Optional[datetime]:
        if self.seconds is None or self.at is None:
            return None
        return self.at + timedelta(seconds=self.seconds)

    @property
    def terminus(self) -> str:
        """행선지 — "동끝행 - 동녘방면" → "동끝"."""
        found = _HEADING.match(self.heading)
        return found.group("end") if found else self.heading

    @property
    def toward(self) -> Optional[str]:
        """다음 역 — "동끝행 - 동녘방면" → "동녘", "솔밭(공원앞)방면" → "솔밭"."""
        found = _HEADING.match(self.heading)
        return re.sub(r"\(.*\)", "", found.group("toward")).strip() if found else None


# "동끝행 - 동녘방면" — 행선지와 다음 역.
_HEADING = re.compile(r"^\s*(?P<end>.+?)행\s*-\s*(?P<toward>.+?)방면\s*$")


def parse_arrivals(body: Dict[str, Any]) -> List[Arrival]:
    out = []
    for row in body.get("realtimeArrivalList") or []:
        seconds: Optional[int]
        try:
            seconds = int(row.get("barvlDt") or 0) or None
        except ValueError:
            seconds = None
        try:
            made = datetime.strptime(str(row.get("recptnDt")), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=KST
            )
        except ValueError:
            made = None
        out.append(
            Arrival(
                line_id=str(row.get("subwayId") or ""),
                updown=str(row.get("updnLine") or ""),
                heading=str(row.get("trainLineNm") or ""),
                station=str(row.get("statnNm") or ""),
                seconds=seconds,
                message=str(row.get("arvlMsg2") or ""),
                at=made,
                current=str(row.get("arvlMsg3") or ""),
                train_no=str(row.get("btrainNo") or ""),
            )
        )
    return out


async def fetch_arrivals(
    client: httpx.AsyncClient, key: str, name: str, rows: int = 30
) -> List[Arrival]:
    """그 역(API 이름)의 도착. 데이터 없음(INFO-200)은 빈 목록, 그 밖의 실패는 예외."""
    response = await client.get(ARRIVALS.format(key=key, rows=rows, name=name))
    if response.status_code != 200:
        raise RuntimeError(f"서울 지하철 HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as error:
        raise RuntimeError(f"서울 지하철 응답을 못 읽음({response.status_code})") from error
    code = str((body.get("errorMessage") or {}).get("code") or body.get("code") or "")
    if code == "INFO-200":
        return []
    if code != "INFO-000":
        message = (body.get("errorMessage") or body).get("message")
        raise RuntimeError(f"서울 지하철 {code} {message}")
    return parse_arrivals(body)


async def api_name(client: httpx.AsyncClient, key: str, station: Station) -> Optional[str]:
    """이 API 가 아는 역 이름. 그대로 물어 나오면 그것, 아니면 모든 역의 도착에서 같은 이름으로
    시작하는 것("가람" → "가람(새터)")."""
    if await fetch_arrivals(client, key, station.base, rows=1):
        return station.base
    every = await fetch_arrivals(client, key, "ALL", rows=1000)
    names = {
        a.station
        for a in every
        if a.line_id == station.line_id and a.station.startswith(f"{station.base}(")
    }
    return sorted(names)[0] if names else None


def usual_way(toward: Dict[str, Optional[Spot]], here: Spot, goal: Spot) -> Optional[str]:
    """평소 방향 — 다음 역이 내리는 곳에 가까워지는 방향(상행·하행·내선·외선). 둘 다면 더 가까워지는
    쪽, 아무 쪽도 아니거나 다음 역 자리를 모르면 None(방향을 모르면 열차를 고르지 않는다).

    리뷰(JAR-044): 두 방향을 다 보여 주니 판단 모델이 반대 방향 열차를 권했다(6번 중 2번 이상)."""
    from_here = meters_between(here, goal)
    closer = {
        way: meters_between(spot, goal)
        for way, spot in toward.items()
        if spot is not None and meters_between(spot, goal) < from_here
    }
    return min(closer, key=lambda way: closer[way]) if closer else None


def render_plan(planned: Sequence[Planned]) -> str:
    """ "동갈래행 07:12쯤(빠듯 — 빨리 닿는 날만 탄다)". 줄마다 행선지 하나·예상 시각 하나 — 시각
    없는 줄("3정거장 전")에서 판단 모델이 시각을 지어냈다(리뷰 JAR-044)."""
    lines = []
    for item in planned:
        how = "빠듯 — 빨리 닿는 날만 탄다" if item.tight else "여유 있게 탄다"
        lines.append(f"- {item.destination}행 {local(item.at):%H:%M}쯤({how})")
    return "\n".join(lines)


class NamedPlaces(Protocol):
    def all(self) -> Sequence[Place]: ...


@dataclass
class TrainArrivals:
    """열차 알림 신호(`train_ahead:<역>:<내리는 곳>:<빠른 HHMM>:<늦은 HHMM>`)에 붙일 탈 열차 — 평소
    방향, 시간표에 지금 늦음을 더한 예상으로 승강장에 닿는 범위와 견줘 "빠듯·여유"를 가른다(못 탈
    열차는 뺀다).

    실시간 두 열차로는 탈 열차를 못 정했다(리뷰 JAR-044 BLOCKED — 늘 역에 닿는 무렵) → 시간표로 세
    번째 열차부터 안다(사용자 결정 2026-11-11). 실시간은 방향과 지금 늦음을 세는 데만 쓴다.
    지하철역이 아니거나, 방향·시간표·탈 열차를 모르거나, 받기가 실패하면 None — 부르는 쪽(루프)이
    말하지 않는다.

    방향은 다음 역의 자리로 정한다 — 카카오에 **역 이름만** 묻는다(사용자의 좌표는 안 나간다).
    """

    client: httpx.AsyncClient
    key: str  # 실시간 지하철 키
    kakao_key: str
    open_key: str  # 서울 열린데이터 일반 인증키(시간표)
    places: NamedPlaces
    _names: Dict[Station, str] = field(default_factory=dict)
    _spots: Dict[Tuple[str, str], Spot] = field(default_factory=dict)
    _codes: Dict[Station, str] = field(default_factory=dict)
    _tables: Dict[Tuple[str, str], Tuple[date, List[Scheduled]]] = field(default_factory=dict)

    async def prepare(self, insight: Insight, now: datetime) -> Optional[ContextBlock]:
        try:
            _, place_id, goal_id, early_at, late_at = insight.trigger.split(":")
            by_id = {p.id: p for p in self.places.all()}
            place, goal = by_id[int(place_id)], by_id[int(goal_id)]
        except (ValueError, KeyError):
            logger.warning("열차 알림의 장소를 못 찾음: %s", insight.trigger)
            return None
        if not place.name:
            logger.info("열차 알림 — 장소 #%s 에 아직 이름이 없다(이름 붙이기 전)", place.id)
            return None
        station = parse_station(place.name)
        if station is None or station.line_id is None:
            logger.info("열차 알림 — 장소 #%s(%s)는 지하철역이 아니다", place.id, place.name)
            return None
        name = self._names.get(station) or await api_name(self.client, self.key, station)
        if name is None:
            logger.warning("열차 알림 — %s 를 실시간 도착에서 못 찾음", place.name)
            return None
        self._names[station] = name
        found = await fetch_arrivals(self.client, self.key, name)
        arrivals = [a for a in found if a.line_id == station.line_id]
        toward: Dict[str, Optional[Spot]] = {}
        for arrival in arrivals:
            if arrival.updown not in toward and arrival.toward:
                toward[arrival.updown] = await self._spot(arrival.toward, station.line)
        way = usual_way(toward, (place.lat, place.lon), (goal.lat, goal.lon))
        if way is None:
            logger.warning("열차 알림 — %s 에서 평소 방향을 못 정함: %s", place.name, toward)
            return None
        table = await self._table(station, way, local(now).date())
        if not table:
            logger.warning("열차 알림 — %s %s 시간표를 못 받음", place.name, way)
            return None
        day = local(now).date()
        live = [(a.train_no, a.arrives) for a in arrivals if a.updown == way and a.arrives]
        late = lateness([(no, at) for no, at in live if at is not None], table, day)
        numbers = {s.train_no for s in table}
        # 시간표와 못 맞추면 라벨에만 "못 맞춤"이 붙고 판단 지시문은 남지 않는다 — 열차번호 꼴이
        # 달라 늘 시간표 그대로 돌아도 아무도 모른다(3차 리뷰). 맞춘 수와 늦음을 남긴다.
        logger.info(
            "열차 알림 — %s %s: 실시간 %d편 중 %d편을 시간표와 맞춤, 지금 늦음 %s",
            place.name,
            way,
            len(live),
            sum(1 for no, _ in live if no in numbers),
            late,
        )
        early = _at(day, early_at) + PLATFORM
        latest = _at(day, late_at) + PLATFORM
        planned = plan(table, late or timedelta(0), early, latest, day)
        if not any(not p.tight for p in planned):
            logger.info("열차 알림 — %s 에 여유 있게 탈 열차가 시간표에 없다", place.name)
            return None
        label = (
            f"{place.name} — 평소 타는 방향, 승강장에 닿는 때 {early:%H:%M}~{latest:%H:%M}쯤(오늘 "
            f"출발 기준). 예상 도착은 시간표{_late_text(late)}({local(now):%H:%M} 기준)"
        )
        return ContextBlock(label=label, body=render_plan(planned))

    async def _spot(self, name: str, line: str) -> Optional[Spot]:
        if (name, line) not in self._spots:
            spot = await station_spot(self.client, self.kakao_key, name, line)
            if spot is None:
                return None  # 못 찾은 것은 기억하지 않는다 — 다음 아침에 다시(리뷰)
            self._spots[(name, line)] = spot
        return self._spots[(name, line)]

    async def _table(self, station: Station, way: str, day: date) -> List[Scheduled]:
        code = self._codes.get(station) or await station_code(
            self.client, self.open_key, station.base, station.line
        )
        if code is None:
            logger.warning("열차 알림 — %s 의 역 번호를 시간표에서 못 찾음", station)
            return []
        self._codes[station] = code
        kept = self._tables.get((code, way))
        if kept is None or kept[0] != day:  # 하루에 한 번 새로
            kept = (day, await timetable(self.client, self.open_key, code, way))
            self._tables[(code, way)] = kept
        return kept[1]


# 개찰구에서 승강장까지 — 탭이 찍힌 곳은 개찰구다(가정 1분, 리뷰 JAR-044).
PLATFORM = timedelta(minutes=1)


def _at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, datetime.strptime(hhmm, "%H%M").time(), tzinfo=KST)


def _late_text(late: Optional[timedelta]) -> str:
    if late is None:
        return " 그대로(실시간 열차와 못 맞춤)"
    minutes = round(late.total_seconds() / 60)
    if minutes == 0:
        return " 그대로(지금 제때 다님)"
    return f"에 지금 {abs(minutes)}분 {'늦게' if minutes > 0 else '일찍'} 다니는 것을 더함"
