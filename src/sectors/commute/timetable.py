"""서울 열린데이터광장 지하철 시간표 — 역 번호 찾기, 평일 시간표, 지금 늦음 더하기, 탈 열차 가르기.

실시간 도착은 방향마다 다음 두 열차만 준다. 버스 탭 순간(역까지 6~7분)에 보이는 두 열차는 출근 시간
배차(2.5~4분) 때문에 거의 늘 역에 닿는 무렵에 와서 "탈 열차"를 정하지 못했다(리뷰 JAR-044 BLOCKED,
사용자 결정 2026-11-11 — 시간표로). 시간표로 세 번째 열차부터 알고, 실시간 열차번호(`btrainNo` =
시간표 `TRAIN_NO`)로 지금 몇 분 늦게 다니는지 세어 더한다.

키는 **일반 인증키**다(실시간 지하철 키와 다르다). 주소 경로에 들어간다 — 로그에서 지운다. HTTP 만
된다. 2026-11-11 시험용 키(`sample`)로 꼴 확인: 가람 → 역 번호 1234(05호선), 평일 하행 198편.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from src.core.clock import KST

logger = logging.getLogger(__name__)

OPEN = "http://openapi.seoul.go.kr:8088/{key}/json/{service}/{start}/{end}/{args}"
STATIONS = "SearchInfoBySubwayNameService"
TIMES = "SearchSTNTimeTableByIDService"
WEEKDAY = 1  # 시간표의 요일 갈래 — 1 평일, 2 토요일, 3 휴일. 열차 알림은 평일에만 운다.
# 실시간과 시간표를 맞춘 늦음이 이보다 크면 다른 회차를 맞춘 것으로 보고 버린다.
MAX_LATE = timedelta(minutes=30)


@dataclass(frozen=True)
class Scheduled:
    train_no: str
    arrives: time  # 시간표의 도착(ARRIVETIME — 첫 역이면 출발)
    destination: str  # 행선지(SUBWAYENAME) — "동갈래"


def line_code(line: str) -> str:
    """이 API 의 노선 이름 — "5호선" → "05호선". 이름 노선(신분당선 등)은 그대로."""
    head = line.removesuffix("호선")
    return f"{int(head):02d}호선" if head.isdigit() else line


async def _open(
    client: httpx.AsyncClient, key: str, service: str, args: str, rows: int
) -> List[Dict[str, Any]]:
    response = await client.get(OPEN.format(key=key, service=service, start=1, end=rows, args=args))
    if response.status_code != 200:
        raise RuntimeError(f"서울 열린데이터 {service} HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as error:
        # 오류는 XML 로 올 때가 있다(`<RESULT><CODE>ERROR-335</CODE>…`).
        raise RuntimeError(
            f"서울 열린데이터 {service} 응답을 못 읽음: {response.text[:120]}"
        ) from error
    part = body.get(service) or body
    result = part.get("RESULT") or {}
    code = str(result.get("CODE") or "")
    if code == "INFO-200":
        return []
    if code != "INFO-000":
        raise RuntimeError(f"서울 열린데이터 {service} {code} {result.get('MESSAGE')}")
    return list(part.get("row") or [])


async def station_code(client: httpx.AsyncClient, key: str, base: str, line: str) -> Optional[str]:
    """역 이름·노선으로 역 번호 — ("가람", "5호선") → "1234". 없으면 None."""
    rows = await _open(client, key, STATIONS, base, 20)
    want = line_code(line)
    for row in rows:
        if row.get("STATION_NM") == base and row.get("LINE_NUM") == want:
            return str(row.get("STATION_CD"))
    return None


def _clock(text: Any) -> Optional[time]:
    try:
        hour, minute, second = (int(part) for part in str(text).split(":"))
    except ValueError:
        return None
    if hour >= 24:  # 자정 넘은 막차("24:10:00") — 아침 알림엔 안 쓴다
        return None
    return time(hour, minute, second)


async def timetable(client: httpx.AsyncClient, key: str, code: str, way: str) -> List[Scheduled]:
    """그 역·방향의 평일 시간표(도착 순). 상행·내선은 1, 하행·외선은 2."""
    inout = 1 if way in ("상행", "내선") else 2
    rows = await _open(client, key, TIMES, f"{code}/{WEEKDAY}/{inout}/", 500)
    found = []
    for row in rows:
        arrives = _clock(row.get("ARRIVETIME"))
        if arrives is None or arrives == time(0):
            arrives = _clock(row.get("LEFTTIME"))  # 첫 역은 도착이 비어 있다
        if arrives is None:
            continue
        found.append(
            Scheduled(str(row.get("TRAIN_NO") or ""), arrives, str(row.get("SUBWAYENAME") or ""))
        )
    return sorted(found, key=lambda s: s.arrives)


def lateness(
    live: Sequence[Tuple[str, datetime]], scheduled: Sequence[Scheduled], day: date
) -> Optional[timedelta]:
    """지금 늦음 — 실시간 열차(열차번호, 예상 도착)를 시간표의 같은 열차와 맞춘 차이의 중앙값.
    맞춘 열차가 없으면 None(시간표대로 본다)."""
    by_no = {s.train_no: s for s in scheduled}
    gaps = []
    for train_no, arrives in live:
        match = by_no.get(train_no)
        if match is None:
            continue
        gap = arrives - datetime.combine(day, match.arrives, tzinfo=KST)
        if abs(gap) <= MAX_LATE:
            gaps.append(gap.total_seconds())
    return timedelta(seconds=median(gaps)) if gaps else None


@dataclass(frozen=True)
class Planned:
    at: datetime  # 예상 도착(시간표 + 지금 늦음)
    destination: str
    tight: bool  # 승강장에 닿는 범위 안 — 빨리 닿는 날만 탄다


def plan(
    scheduled: Sequence[Scheduled],
    late: timedelta,
    early: datetime,
    latest: datetime,
    day: date,
    roomy: int = 2,
) -> List[Planned]:
    """승강장에 닿는 범위(`early`~`latest`)로 열차를 가른다 — 그 앞은 못 탐(뺀다), 범위 안은 빠듯,
    뒤는 여유. 빠듯한 열차 다음에 여유 있는 열차 `roomy` 개까지."""
    out: List[Planned] = []
    for item in scheduled:
        at = datetime.combine(day, item.arrives, tzinfo=KST) + late
        if at < early:
            continue
        out.append(Planned(at, item.destination, tight=at < latest))
        if sum(1 for p in out if not p.tight) >= roomy:
            break
    return out
