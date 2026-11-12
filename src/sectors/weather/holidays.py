"""특일 정보(한국천문연구원, 공공데이터포털) — 한 해의 공휴일.

달을 빼고 해만 주면 그해 공휴일이 한 번에 온다(2026-11-11 실측: 2026년 22일 — 대체공휴일·선거일
포함). 한 날이면 `item` 이 목록이 아니라 하나로 온다(2026-11-09 12월만 물었을 때) — 둘 다 읽는다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List

import httpx

from src.sectors.weather.kma import service_key

HOLIDAY_URL = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"


def parse_holidays(body: Dict[str, Any]) -> Dict[date, str]:
    """응답의 `response` 아래를 받아 공휴일(isHoliday=Y)만 날짜 → 이름으로."""
    items = (body.get("body") or {}).get("items") or {}
    item = items.get("item") if isinstance(items, dict) else None
    rows: List[Dict[str, Any]] = item if isinstance(item, list) else ([item] if item else [])
    days: Dict[date, str] = {}
    for row in rows:
        if str(row.get("isHoliday", "")).upper() != "Y":
            continue
        try:
            day = datetime.strptime(str(row["locdate"]), "%Y%m%d").date()
        except (KeyError, ValueError):
            continue
        name = str(row.get("dateName") or "공휴일")
        # 같은 날 둘(설날과 대체공휴일 등)이면 이어 적는다.
        days[day] = f"{days[day]}·{name}" if day in days and name not in days[day] else name
    return days


async def fetch_holidays(client: httpx.AsyncClient, key: str, year: int) -> Dict[date, str]:
    """그해 공휴일. 응답이 정상이 아니거나 하나도 없으면 예외 — 빈 표로 덮지 않는다."""
    response = await client.get(
        HOLIDAY_URL,
        params={"serviceKey": service_key(key), "solYear": year, "_type": "json", "numOfRows": 100},
    )
    if response.status_code != 200:
        raise RuntimeError(f"특일 정보 HTTP {response.status_code}: {response.text[:200]}")
    try:
        body = response.json()["response"]
        header = body["header"]
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"특일 정보 응답을 못 읽음: {response.text[:200]}") from error
    if header.get("resultCode") != "00":
        raise RuntimeError(f"특일 정보 {header.get('resultCode')} {header.get('resultMsg')}")
    days = parse_holidays(body)
    if not days:
        raise RuntimeError(f"{year}년 공휴일이 하나도 안 왔다 — 아직 안 나왔거나 응답이 이상하다")
    return days
