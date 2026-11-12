"""weather 섹터 — 예보 읽기·받기, 공휴일, 맥락 한 줄, 비·눈 신호, 가져오기."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from src.core.clock import KST
from src.core.days import is_workday
from src.core.models import Observation
from src.runtime.pull import NotReady
from src.sectors.weather.holidays import fetch_holidays, parse_holidays
from src.sectors.weather.kma import (
    Hour,
    base_time,
    fetch_forecast,
    grid,
    parse_forecast,
    service_key,
)
from src.sectors.weather.metrics import KIND, describe_forecast, hours_of, wet_spans
from src.sectors.weather.pull import ForecastPull, HolidayPull
from src.sectors.weather.triggers import RainAheadTrigger
from src.storage.days import SQLiteHolidays
from src.storage.sqlite import SQLiteStore

K = lambda *a: datetime(*a, tzinfo=KST)  # noqa: E731


def _items(rows: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    """{"2026111207": {"TMP": "5", "PTY": "1", "POP": "70"}} → 기상청 줄들."""
    out = []
    for stamp, values in rows.items():
        for category, value in values.items():
            out.append(
                {
                    "baseDate": "20261111",
                    "baseTime": "2000",
                    "category": category,
                    "fcstDate": stamp[:8],
                    "fcstTime": f"{stamp[8:]}00",
                    "fcstValue": value,
                    "nx": 60,
                    "ny": 127,
                }
            )
    return out


def _hour(at: datetime, pty: int = 0, pop: int = 10, temp: float = 10.0) -> Hour:
    return Hour(at=at, temp=temp, pop=pop, pty=pty, pcp="강수없음", sno="적설없음", sky=1)


def _forecast(at: datetime, hours: List[Hour]) -> Observation:
    return Observation(
        source="kma",
        kind=KIND,
        value=float(max((h.pop or 0) for h in hours)),
        at=at,
        meta={"grid": [60, 127], "hours": [h.to_meta() for h in hours]},
    )


# ── 기상청 단기예보 ─────────────────────────────────────────────────


def test_the_grid_matches_the_published_points() -> None:
    """기상청이 알려 둔 격자 — 서울 시청 60,127, 강남 61,125, 부산 98,76, 제주 53,38."""
    assert grid(37.5665, 126.9780) == (60, 127)
    assert grid(37.4979, 127.0276) == (61, 125)
    assert grid(35.1796, 129.0756) == (98, 76)
    assert grid(33.4996, 126.5312) == (53, 38)


def test_the_latest_release_is_ten_minutes_after_the_hour() -> None:
    assert base_time(K(2026, 11, 11, 11, 9)) == K(2026, 11, 11, 8, 0)
    assert base_time(K(2026, 11, 11, 11, 10)) == K(2026, 11, 11, 11, 0)
    assert base_time(K(2026, 11, 11, 2, 9)) == K(
        2026, 11, 10, 23, 0
    )  # 새벽 2시 발표 전엔 어젯밤 것
    # 서버의 시각은 UTC 다 — 한국 시각으로 바꿔 센다(11/10 17:30 UTC = 11/11 02:30 KST).
    assert base_time(datetime(2026, 11, 10, 17, 30, tzinfo=timezone.utc)) == K(2026, 11, 11, 2, 0)


def test_the_key_is_decoded_only_when_encoded() -> None:
    """포털이 준 "인코딩" 꼴 그대로 보내니 "등록되지 않은 서비스키"였다(2026-11-09)."""
    assert service_key("ab%2Bcd%3D%3D") == "ab+cd=="
    assert service_key("ab+cd==") == "ab+cd=="


def test_forecast_lines_become_hours() -> None:
    rows = _items(
        {
            "2026111207": {"TMP": "5", "PTY": "1", "POP": "70", "PCP": "1.0mm", "SKY": "4"},
            "2026111208": {"TMP": "6", "PTY": "0", "POP": "30", "PCP": "강수없음", "SKY": "3"},
            "2026111209": {"UUU": "1.2"},  # 기온·강수가 다 빠진 시간은 버린다
        }
    )
    hours = parse_forecast(rows)
    assert [h.at for h in hours] == [K(2026, 11, 12, 7), K(2026, 11, 12, 8)]
    assert hours[0].wet and hours[0].pty == 1 and hours[0].pop == 70 and hours[0].pcp == "1.0mm"
    assert not hours[1].wet
    assert _hour(K(2026, 11, 12, 9), pty=0, pop=60).wet  # 강수형태가 없어도 60% 면 올 수 있다
    assert Hour.from_meta(hours[0].to_meta()) == hours[0]


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_the_forecast_is_fetched_with_the_decoded_key_and_fails_loudly() -> None:
    seen: List[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"}}}
        body["response"]["body"] = {"items": {"item": _items({"2026111207": {"TMP": "5"}})}}
        return httpx.Response(200, json=body)

    hours = asyncio.run(fetch_forecast(_client(ok), "ab%2Bcd", 60, 127, K(2026, 11, 11, 20)))
    assert len(hours) == 1
    params = seen[0].url.params
    assert params["serviceKey"] == "ab+cd" and params["base_time"] == "2000"
    assert params["base_date"] == "20261111" and params["nx"] == "60"

    def no_data(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"response": {"header": {"resultCode": "03", "resultMsg": "NO_DATA"}}}
        )

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="SERVICE_KEY_IS_NOT_REGISTERED_ERROR")

    def empty(request: httpx.Request) -> httpx.Response:
        head = {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"}
        return httpx.Response(200, json={"response": {"header": head, "body": {"items": ""}}})

    for handler, words in ((no_data, "NO_DATA"), (refused, "403"), (empty, "비었다")):
        with pytest.raises(RuntimeError, match=words):
            asyncio.run(fetch_forecast(_client(handler), "k", 60, 127, K(2026, 11, 11, 20)))


# ── 공휴일 ─────────────────────────────────────────────────────────


def test_holidays_are_read_from_a_list_or_a_single_item() -> None:
    """한 날만 오면 item 이 목록이 아니라 하나로 온다(2026-11-09 12월만 물었을 때)."""
    many = {
        "body": {
            "items": {
                "item": [
                    {"locdate": 20260216, "dateName": "설날", "isHoliday": "Y"},
                    {"locdate": 20260302, "dateName": "대체공휴일(삼일절)", "isHoliday": "Y"},
                    {"locdate": 20260308, "dateName": "기념일", "isHoliday": "N"},
                ]
            }
        }
    }
    assert parse_holidays(many) == {
        date(2026, 2, 16): "설날",
        date(2026, 3, 2): "대체공휴일(삼일절)",
    }
    one = {
        "body": {
            "items": {"item": {"locdate": 20261225, "dateName": "기독탄신일", "isHoliday": "Y"}}
        }
    }
    assert parse_holidays(one) == {date(2026, 12, 25): "기독탄신일"}
    assert parse_holidays({"body": {"items": ""}}) == {}


def test_a_year_without_holidays_is_an_error_not_an_empty_table() -> None:
    def empty(request: httpx.Request) -> httpx.Response:
        head = {"resultCode": "00", "resultMsg": "NORMAL SERVICE."}
        return httpx.Response(200, json={"response": {"header": head, "body": {"items": ""}}})

    with pytest.raises(RuntimeError, match="하나도"):
        asyncio.run(fetch_holidays(_client(empty), "k", 2027))


# ── 맥락 한 줄 ─────────────────────────────────────────────────────


def test_the_context_line_tells_today_and_tomorrow_with_dates() -> None:
    now = K(2026, 11, 11, 14, 30)
    hours = [_hour(K(2026, 11, 11, h), temp=10 + h % 3) for h in range(11, 24)]
    hours += [
        _hour(
            K(2026, 11, 12, h),
            pty=1 if 7 <= h <= 9 else 0,
            pop=70 if 7 <= h <= 9 else 20,
            temp=3 + h // 3,
        )
        for h in range(0, 24)
    ]
    line = describe_forecast(_forecast(K(2026, 11, 11, 11), hours), now)
    assert line.startswith("11/11 11시 발표 — 오늘 11/11(수) 14~23시")  # 지난 시간은 빼고
    assert "비·눈 없음" in line.split(" · ")[0]
    assert "내일 11/12(목) 00~23시 3~10℃, 07~09시 비(강수확률 최고 70%)" in line


def test_wet_hours_are_grouped_when_they_follow_each_other() -> None:
    hours = [
        _hour(K(2026, 11, 12, 7), pty=1, pop=70),
        _hour(K(2026, 11, 12, 8), pty=1, pop=80),
        _hour(K(2026, 11, 12, 12), pty=0, pop=60),
        _hour(K(2026, 11, 12, 18), pty=3, pop=60),
    ]
    assert wet_spans(hours) == [
        "07~08시 비(강수확률 최고 80%)",
        "12시 비 올 수도(강수확률 최고 60%)",
        "18시 눈(강수확률 최고 60%)",
    ]


# ── 비·눈 신호 ─────────────────────────────────────────────────────


def _rain_on(day: date, base: datetime) -> Observation:
    hours = [
        _hour(datetime.combine(day, datetime.min.time(), tzinfo=KST) + timedelta(hours=h))
        for h in range(24)
    ]
    hours[8] = _hour(hours[8].at, pty=1, pop=70)
    hours[9] = _hour(hours[9].at, pty=1, pop=80)
    hours[15] = _hour(hours[15].at, pty=1, pop=90)  # 오후 비 — 내일 아침 신호엔 안 들어간다
    return _forecast(base, hours)


def test_rain_tomorrow_morning_is_raised_in_the_evening_once_per_day() -> None:
    trigger = RainAheadTrigger()
    forecast = _rain_on(date(2026, 11, 12), K(2026, 11, 11, 20))
    insight = trigger.check([forecast], K(2026, 11, 11, 20, 30))
    assert insight is not None and insight.family == "weather" and insight.trigger == "rain_ahead"
    assert insight.at == K(2026, 11, 12, 0)  # 그날 0시 — 같은 날은 게이트가 한 번만
    assert "내일 아침(06~10시) 11/12(목)" in insight.summary
    assert "08~09시 비(강수확률 최고 80%)" in insight.summary and "15시" not in insight.summary
    # 다음 날 아침에 같은 날을 보면 신호의 at 이 같다 — 저녁에 판단했으면 다시 안 묻는다.
    fresh = _rain_on(date(2026, 11, 12), K(2026, 11, 12, 5))
    morning = trigger.check([forecast, fresh], K(2026, 11, 12, 7, 0))  # 가장 최근 발표를 본다
    assert morning is not None and morning.at == insight.at
    assert "오늘 낮(07~21시)" in morning.summary and "15시" in morning.summary


def test_no_rain_signal_outside_the_hours_or_from_an_old_forecast() -> None:
    trigger = RainAheadTrigger()
    forecast = _rain_on(date(2026, 11, 12), K(2026, 11, 11, 20))
    assert trigger.check([forecast], K(2026, 11, 11, 14, 0)) is None  # 저녁·아침이 아니면
    assert trigger.check([], K(2026, 11, 11, 20, 30)) is None
    stale = _rain_on(date(2026, 11, 12), K(2026, 11, 11, 11))
    assert trigger.check([stale], K(2026, 11, 11, 20, 30)) is None  # 9시간 묵은 예보
    dry = _forecast(K(2026, 11, 11, 20), [_hour(K(2026, 11, 12, h)) for h in range(24)])
    assert trigger.check([dry], K(2026, 11, 11, 21, 0)) is None


# ── 가져오기 ───────────────────────────────────────────────────────


def _forecast_server(calls: List[httpx.Request]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        rows = {
            f"2026111{2 if h >= 24 else 1}{h % 24:02d}": {
                "TMP": "7",
                "PTY": "0",
                "POP": "70" if h == 30 else "20",
            }
            for h in range(21, 45)
        }
        body = {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                "body": {"items": {"item": _items(rows)}},
            }
        }
        return httpx.Response(200, json=body)

    return handler


def test_the_forecast_pull_writes_one_observation_per_release(tmp_path: Path) -> None:
    calls: List[httpx.Request] = []
    store = SQLiteStore(tmp_path / "t.db")
    spot: List[Optional[Tuple[float, float]]] = [None]
    pull = ForecastPull(
        client=_client(_forecast_server(calls)), key="k", where=lambda: spot[0], store=store
    )
    now = K(2026, 11, 11, 20, 30)
    with pytest.raises(NotReady, match="집을 아직 모른다"):
        asyncio.run(pull.pull(now))
    spot[0] = (37.5665, 126.9780)
    said = asyncio.run(pull.pull(now))
    assert "11/11 20시 발표" in said and "격자 60,127" in said and "70%" in said
    [written] = store.recent(KIND, now - timedelta(days=1))
    assert written.at == K(2026, 11, 11, 20) and written.value == 70
    assert written.meta["grid"] == [60, 127] and len(hours_of(written)) == 24
    assert asyncio.run(pull.pull(now + timedelta(hours=1))) == "11/11 20시 발표 그대로"
    assert len(calls) == 1
    asyncio.run(pull.pull(K(2026, 11, 11, 23, 15)))  # 새 발표
    assert len(calls) == 2


def _holiday_server(calls: List[str], fail_next: bool = False) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        year = request.url.params["solYear"]
        calls.append(year)
        head = {"resultCode": "00", "resultMsg": "NORMAL SERVICE."}
        if fail_next and year == "2027":
            return httpx.Response(200, json={"response": {"header": head, "body": {"items": ""}}})
        item = {"locdate": int(f"{year}1225"), "dateName": "기독탄신일", "isHoliday": "Y"}
        return httpx.Response(
            200, json={"response": {"header": head, "body": {"items": {"item": item}}}}
        )

    return handler


def test_the_holiday_pull_fills_the_table_and_refreshes_daily(tmp_path: Path) -> None:
    calls: List[str] = []
    store = SQLiteHolidays(tmp_path / "t.db")
    pull = HolidayPull(client=_client(_holiday_server(calls)), key="k", store=store)
    june = K(2026, 6, 1, 9)
    said = asyncio.run(pull.pull(june))
    assert calls == ["2026"] and "2026년 1일" in said
    assert not is_workday(date(2026, 12, 25))  # 표가 바로 올라간다
    assert asyncio.run(pull.pull(june + timedelta(hours=12))).startswith("새로 받을 해 없음")
    # 하루가 지나면 다시 — 임시공휴일은 2~4주 전에 정해진다(리뷰 JAR-043: 30일로는 못 받고 지나갈 수
    # 있었다).
    asyncio.run(pull.pull(june + timedelta(days=1, minutes=1)))
    assert calls == ["2026", "2026"]


def test_next_years_holidays_from_november_and_a_missing_table_is_tolerated(tmp_path: Path) -> None:
    calls: List[str] = []
    store = SQLiteHolidays(tmp_path / "t.db")
    pull = HolidayPull(client=_client(_holiday_server(calls, fail_next=True)), key="k", store=store)
    said = asyncio.run(pull.pull(K(2026, 11, 11, 9)))
    assert calls == ["2026", "2027"]
    assert "2026년 1일" in said and "2027년 못 받음" in said  # 내년 표가 아직이면 다음에

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    fresh = HolidayPull(client=_client(broken), key="k", store=SQLiteHolidays(tmp_path / "u.db"))
    with pytest.raises(RuntimeError, match="500"):  # 올해 것이 안 오면 시끄럽게
        asyncio.run(fresh.pull(K(2026, 11, 11, 9)))


def test_forecast_meta_is_plain_json(tmp_path: Path) -> None:
    """관측치 meta 는 JSON 으로 저장된다 — 시간이 글로 들어가야 다시 읽힌다."""
    observation = _rain_on(date(2026, 11, 12), K(2026, 11, 11, 20))
    assert json.loads(json.dumps(observation.meta))["hours"][8]["pty"] == 1


def test_a_morning_rain_signal_does_not_lean_on_hours_already_gone() -> None:
    """08:40 에 05시 발표만 있으면 "07시 비"가 근거가 될 수 있었다(리뷰 JAR-043)."""
    hours = [
        _hour(K(2026, 11, 12, h), pty=1 if h == 7 else 0, pop=70 if h == 7 else 10)
        for h in range(24)
    ]
    forecast = _forecast(K(2026, 11, 12, 5), hours)
    assert RainAheadTrigger().check([forecast], K(2026, 11, 12, 7, 30)) is not None
    assert RainAheadTrigger().check([forecast], K(2026, 11, 12, 8, 40)) is None
