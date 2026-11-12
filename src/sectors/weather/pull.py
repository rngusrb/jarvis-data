"""서버가 가져오는 것 — 기상청 예보(한 시간마다 새 발표가 있나 보고)와 공휴일(해마다).

언제 돌릴지와 실패를 견디는 건 `src/runtime/pull.py` 가 맡는다. 여기는 한 번 가져오는 일만 한다.
예보를 어디의 것으로 받을지(`where`)는 조립(`app/main.py`)이 이동 섹터의 "집"을 넘겨준다 —
섹터끼리는 서로 모른다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

import httpx

from src.core.clock import local
from src.core.days import set_holidays
from src.core.models import Observation
from src.runtime.pull import NotReady
from src.sectors.weather.holidays import fetch_holidays
from src.sectors.weather.kma import base_time, fetch_forecast, grid
from src.sectors.weather.metrics import KIND

logger = logging.getLogger(__name__)

SOURCE = "kma"


class ObservationWriter(Protocol):
    def write(self, observations: Iterable[Observation]) -> int: ...


class HolidayStore(Protocol):
    def save(self, year: int, days: Mapping[date, str], fetched_at: datetime) -> int: ...

    def load(self) -> Dict[date, str]: ...

    def fetched(self) -> Dict[int, datetime]: ...


@dataclass
class ForecastPull:
    client: httpx.AsyncClient
    key: str
    # 예보를 받을 곳(위도, 경도) — 모르면 None.
    where: Callable[[], Optional[Tuple[float, float]]]
    store: ObservationWriter
    name: str = "기상청 예보"
    # 발표는 3시간마다지만 한 시간마다 본다 — 새 발표가 없으면 묻지 않는다.
    every: timedelta = timedelta(hours=1)
    _last: Optional[Tuple[datetime, int, int]] = None

    async def pull(self, now: datetime) -> str:
        spot = self.where()
        if spot is None:
            raise NotReady(
                "집을 아직 모른다 — 평일 아침 '집 나섬'이 사흘은 찍혀야 집 근처 날씨를 받는다"
            )
        nx, ny = grid(*spot)
        base = base_time(now)
        if self._last == (base, nx, ny):
            return f"{base:%m/%d %H}시 발표 그대로"
        hours = await fetch_forecast(self.client, self.key, nx, ny, base)
        ahead = [
            h.pop or 0
            for h in hours
            if now <= h.at + timedelta(hours=1) <= now + timedelta(hours=25)
        ]
        observation = Observation(
            source=SOURCE,
            kind=KIND,
            value=float(max(ahead, default=0)),
            at=base,
            meta={"grid": [nx, ny], "hours": [h.to_meta() for h in hours]},
        )
        self.store.write([observation])
        self._last = (base, nx, ny)
        return (
            f"{base:%m/%d %H}시 발표 {len(hours)}시간(격자 {nx},{ny}, 24시간 최고 강수확률 "
            f"{observation.value:g}%)"
        )


@dataclass
class HolidayPull:
    client: httpx.AsyncClient
    key: str
    store: HolidayStore
    name: str = "공휴일"
    every: timedelta = timedelta(hours=12)
    # 받은 해도 이만큼 지나면 다시 — 임시공휴일은 2~4주 전에 정해진다(2025-01-27 은 19일 전). 하루
    # 한 번이면 한 해 표를 한 번 더 묻는 것뿐이다(리뷰 JAR-043 — 처음 30일로는 못 받고 지나갈 수
    # 있었다).
    refresh_after: timedelta = timedelta(days=1)

    async def pull(self, now: datetime) -> str:
        today = local(now).date()
        # 11월부터는 내년 것도 — 연말에 다음 해 표가 비어 있지 않게.
        years = [today.year] + ([today.year + 1] if today.month >= 11 else [])
        fetched = self.store.fetched()
        done: List[str] = []
        for year in years:
            last = fetched.get(year)
            if last is not None and now - last < self.refresh_after:
                continue
            try:
                days = await fetch_holidays(self.client, self.key, year)
            except RuntimeError as error:
                if year == today.year:
                    raise
                # 내년 표는 아직 안 나왔을 수 있다 — 올해 것은 그대로 쓰고 다음에 다시.
                logger.warning("%d년 공휴일을 못 받았다 — 다음에 다시: %s", year, error)
                done.append(f"{year}년 못 받음({error})")
                continue
            self.store.save(year, days, now)
            done.append(f"{year}년 {len(days)}일")
        loaded = self.store.load()
        set_holidays(loaded)
        return (", ".join(done) or "새로 받을 해 없음") + f" — 표에 {len(loaded)}일"
