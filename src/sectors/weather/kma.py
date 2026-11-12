"""기상청 단기예보(공공데이터포털) — 격자 바꾸기, 발표 시각, 받기, 읽기.

단기예보는 하루 여덟 번(02·05·08·11·14·17·20·23시) 발표되고, 발표 10분 뒤부터 받을 수 있다. 한 번에
그때부터 사흘 남짓의 시간별 값이 온다(2026-11-11 실측: 11시 발표 69시간 835줄, 한 쪽 1000줄로 다
온다). 좌표가 아니라 5km 격자(nx, ny)로 묻는다 — 집의 정확한 좌표는 밖으로 안 나간다.

읽기(`parse_forecast`)는 순수 함수, 받기(`fetch_forecast`)만 네트워크를 쓴다. 키는 포털이 주는
"인코딩" 꼴로 `.env` 에 들어 있어 풀어서 보낸다(`service_key` — 2026-11-09 그대로 보내니 "등록되지
않은 서비스키").
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote

import httpx

from src.core.clock import KST, local

FORECAST_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
BASE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
# 발표 뒤 이만큼 지나야 받을 수 있다(기상청 안내 — 그 전엔 NO_DATA).
READY_AFTER = timedelta(minutes=10)
# 강수형태 — 0 은 없음.
RAIN_KINDS = {1: "비", 2: "비/눈", 3: "눈", 4: "소나기"}
# 강수형태가 0 이어도 이만큼이면 "올 수도"로 친다.
LIKELY_POP = 60


def service_key(raw: str) -> str:
    """포털이 준 키를 보낼 꼴로 — 인코딩된 꼴(%2B…)이면 푼다. 요청할 때 httpx 가 다시 싼다."""
    return unquote(raw) if "%" in raw else raw


def grid(lat: float, lon: float) -> Tuple[int, int]:
    """위경도 → 기상청 격자(람베르트 정각원추, 5km). 기상청이 공개한 변환식 그대로."""
    re_, step = 6371.00877, 5.0
    slat1, slat2, olon, olat = (math.radians(v) for v in (30.0, 60.0, 126.0, 38.0))
    xo, yo = 43, 136
    radius = re_ / step
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    )
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5) ** sn * math.cos(slat1) / sn
    ro = radius * sf / math.tan(math.pi * 0.25 + olat * 0.5) ** sn
    ra = radius * sf / math.tan(math.pi * 0.25 + math.radians(lat) * 0.5) ** sn
    theta = math.radians(lon) - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn
    return (
        int(math.floor(ra * math.sin(theta) + xo + 0.5)),
        int(math.floor(ro - ra * math.cos(theta) + yo + 0.5)),
    )


def base_time(now: datetime) -> datetime:
    """지금 받을 수 있는 가장 최근 발표 시각(한국 시각)."""
    ready = local(now).astimezone(KST) - READY_AFTER
    for hour in reversed(BASE_HOURS):
        if ready.hour >= hour:
            return ready.replace(hour=hour, minute=0, second=0, microsecond=0)
    return (ready - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class Hour:
    """한 시간의 예보."""

    at: datetime  # 한국 시각
    temp: Optional[float]  # 기온 ℃ (TMP)
    pop: Optional[int]  # 강수확률 % (POP)
    pty: int  # 강수형태 (PTY) — RAIN_KINDS
    pcp: str  # 1시간 강수량 — 기상청이 준 글 그대로("강수없음", "1mm 미만", "3.0mm", "1")
    sno: str  # 1시간 신적설 — 글 그대로
    sky: Optional[int]  # 하늘 1 맑음, 3 구름많음, 4 흐림

    @property
    def wet(self) -> bool:
        """비·눈이 오거나 올 수 있는 시간."""
        return self.pty in RAIN_KINDS or (self.pop or 0) >= LIKELY_POP

    def to_meta(self) -> Dict[str, Any]:
        row = asdict(self)
        row["at"] = self.at.isoformat()
        return row

    @staticmethod
    def from_meta(row: Dict[str, Any]) -> "Hour":
        return Hour(
            at=datetime.fromisoformat(str(row["at"])),
            temp=_float(row.get("temp")),
            pop=_int(row.get("pop")),
            pty=_int(row.get("pty")) or 0,
            pcp=str(row.get("pcp") or ""),
            sno=str(row.get("sno") or ""),
            sky=_int(row.get("sky")),
        )


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    number = _float(value)
    return int(number) if number is not None else None


def parse_forecast(items: Iterable[Dict[str, Any]]) -> List[Hour]:
    """단기예보의 줄(항목 하나에 한 줄)을 시간별로 모은다. 기온·강수가 다 빠진 시간은 버린다."""
    by_hour: Dict[datetime, Dict[str, str]] = {}
    for item in items:
        try:
            at = datetime.strptime(f"{item['fcstDate']}{item['fcstTime']}", "%Y%m%d%H%M")
        except (KeyError, ValueError):
            continue
        by_hour.setdefault(at.replace(tzinfo=KST), {})[str(item.get("category"))] = str(
            item.get("fcstValue", "")
        )
    hours = []
    for at, values in sorted(by_hour.items()):
        if not {"TMP", "PTY", "POP"} & values.keys():
            continue
        hours.append(
            Hour(
                at=at,
                temp=_float(values.get("TMP")),
                pop=_int(values.get("POP")),
                pty=_int(values.get("PTY")) or 0,
                pcp=values.get("PCP", ""),
                sno=values.get("SNO", ""),
                sky=_int(values.get("SKY")),
            )
        )
    return hours


async def fetch_forecast(
    client: httpx.AsyncClient, key: str, nx: int, ny: int, base: datetime
) -> List[Hour]:
    """그 발표의 시간별 예보. 응답이 정상이 아니면 무엇이 왔는지와 함께 예외 — 조용히 빈 예보를
    내지 않는다."""
    response = await client.get(
        FORECAST_URL,
        params={
            "serviceKey": service_key(key),
            "pageNo": 1,
            "numOfRows": 1000,
            "dataType": "JSON",
            "base_date": base.strftime("%Y%m%d"),
            "base_time": base.strftime("%H%M"),
            "nx": nx,
            "ny": ny,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"기상청 예보 HTTP {response.status_code}: {response.text[:200]}")
    try:
        body = response.json()["response"]
        header = body["header"]
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"기상청 예보 응답을 못 읽음: {response.text[:200]}") from error
    if header.get("resultCode") != "00":
        raise RuntimeError(f"기상청 예보 {header.get('resultCode')} {header.get('resultMsg')}")
    items: Sequence[Dict[str, Any]] = ((body.get("body") or {}).get("items") or {}).get(
        "item"
    ) or []
    hours = parse_forecast(items)
    if not hours:
        raise RuntimeError(f"기상청 예보가 비었다({base:%m/%d %H}시 발표, 격자 {nx},{ny})")
    return hours
