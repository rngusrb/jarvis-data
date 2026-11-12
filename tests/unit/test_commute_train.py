"""장소 이름(카카오)과 출근 열차(서울 실시간 지하철) — JAR-044."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import pytest

from src.core.days import set_holidays
from src.core.models import Event, Insight, Severity
from src.core.places import Place
from src.sectors.commute.kakao import nearest_station, region
from src.sectors.commute.naming import PlaceNaming
from src.sectors.commute.subway import (
    TrainArrivals,
    api_name,
    fetch_arrivals,
    parse_arrivals,
    parse_station,
    render_plan,
    usual_way,
)
from src.sectors.commute.timetable import lateness, line_code, plan, station_code, timetable
from src.sectors.commute.triggers import TrainAheadTrigger

KST = timezone(timedelta(hours=9))
MONDAY = datetime(2026, 9, 7, tzinfo=KST)
HOME, BUS, GARAM, NARU, ELSEWHERE = 3, 4, 5, 6, 9


def _at(day: int, hour: int, minute: int, place: int, label: str = "wallet") -> Event:
    when = MONDAY + timedelta(days=day, hours=hour, minutes=minute)
    return Event(source="s", kind="location", at=when, label=label, place_id=place)


def _bus_then_subway(days: List[int], ride: int = 6) -> List[Event]:
    """2026-11 실측 그대로 — 집 → 버스(다라동) → 가람역 5호선에서 갈아타고 → 나루."""
    out: List[Event] = []
    for d in days:
        out += [
            _at(d, 7, 0, HOME, "leave_home"),
            _at(d, 7, 3, BUS),
            _at(d, 7, 3 + ride, GARAM),
            _at(d, 7, 43, NARU),
            _at(d, 18, 0, NARU),
        ]
    return out


# ── 열차 알림 신호 ─────────────────────────────────────────────────


def test_the_train_signal_comes_right_after_boarding_the_bus_with_the_arrival_window() -> None:
    """역에 닿는 때는 그날 범위다 — 최근 걸린 시간의 가장 짧은~가장 긴 것(중앙값 하나로는 탈 열차를
    못 가렸다 — 리뷰 JAR-044)."""
    past = [e for d, ride in enumerate((5, 6, 7, 8)) for e in _bus_then_subway([d], ride=ride)]
    today = [_at(4, 7, 0, HOME, "leave_home"), _at(4, 7, 3, BUS)]
    insight = TrainAheadTrigger().check(
        past + today, MONDAY + timedelta(days=4, hours=7, minutes=4)
    )
    assert insight is not None and insight.family == "train"
    assert insight.severity == Severity.NOTABLE
    # 역의 장소, 방향을 정할 내리는 곳, 역에 닿는 범위(07:03 + 5~8분).
    assert insight.trigger == f"train_ahead:{GARAM}:{NARU}:0708:0711"
    assert insight.at == today[1].at  # 그 아침 한 번
    assert "07:03 아침에 타는 곳에서 출발" in insight.summary and "07:08~07:11쯤" in insight.summary
    assert "장소 #" not in insight.summary  # 번호가 말로 샜다


@pytest.mark.parametrize(
    "minute, why",
    [(15, "출발한 지 10분이 넘으면 늦었다"), (9, "빨리 닿는 날이면 이미 역이다")],
)
def test_no_train_signal_when_it_is_too_late(minute: int, why: str) -> None:
    events = _bus_then_subway([0, 1, 2, 3]) + [_at(4, 7, 3, BUS)]
    now = MONDAY + timedelta(days=4, hours=7, minutes=minute)
    assert TrainAheadTrigger().check(events, now) is None, why


def test_no_train_signal_on_a_holiday_elsewhere_or_without_a_usual() -> None:
    now = MONDAY + timedelta(days=4, hours=7, minutes=4)
    events = _bus_then_subway([0, 1, 2, 3]) + [_at(4, 7, 3, BUS)]
    set_holidays({date(2026, 9, 11): "임시공휴일"})
    assert TrainAheadTrigger().check(events, now) is None
    set_holidays({})
    other = _bus_then_subway([0, 1, 2, 3]) + [_at(4, 7, 3, ELSEWHERE)]
    assert TrainAheadTrigger().check(other, now) is None  # 평소와 다른 곳에서 탔다
    few = _bus_then_subway([2, 3]) + [_at(4, 7, 3, BUS)]
    assert TrainAheadTrigger().check(few, now) is None  # 평소가 사흘이 안 된다
    # 출발 10분이 지나면 늦었다 — 역까지 오래 걸리는 날(20분)에도(리뷰: 10분을 30분으로 바꿔도
    # 시험이 통과했다 — 역 1분 전 검사가 가렸다).
    slow = _bus_then_subway([0, 1, 2, 3], ride=20) + [_at(4, 7, 3, BUS)]
    assert (
        TrainAheadTrigger().check(slow, MONDAY + timedelta(days=4, hours=7, minutes=12)) is not None
    )
    assert TrainAheadTrigger().check(slow, MONDAY + timedelta(days=4, hours=7, minutes=14)) is None


def test_a_direct_subway_rider_hears_it_when_leaving_home() -> None:
    """갈아타는 곳이 없으면 타는 곳이 역이고, 집을 나선 때가 출발이다."""
    past = [
        e
        for d in range(4)
        for e in (
            _at(d, 7, 0, HOME, "leave_home"),
            _at(d, 7, 10, GARAM),
            _at(d, 7, 40, NARU),
        )
    ]
    insight = TrainAheadTrigger().check(
        past + [_at(4, 7, 0, HOME, "leave_home")], MONDAY + timedelta(days=4, hours=7, minutes=1)
    )
    assert insight is not None and insight.trigger == f"train_ahead:{GARAM}:{NARU}:0710:0710"
    assert "집을 나섬에서 출발" in insight.summary


def _tap(day: int, hour: int, minute: int, second: int, place: int) -> Event:
    when = MONDAY + timedelta(days=day, hours=hour, minutes=minute, seconds=second)
    return Event(source="s", kind="location", at=when, label="wallet", place_id=place)


def test_the_window_is_rounded_up_to_the_minute() -> None:
    """3차 리뷰 재현 — 신호에 분까지만 실으며 내렸더니 늦은 끝 07:12:50 이 07:12 가 되어, 그 사이
    열차가 "여유"로 나왔다. 둘 다 올린다(덜 약속하는 쪽)."""
    past = []
    for day, seconds in enumerate((310, 360, 470, 400)):  # 5:10 ~ 7:50
        past += [
            _at(day, 7, 0, HOME, "leave_home"),
            _tap(day, 7, 3, 0, BUS),
            _tap(day, 7, 3, seconds, GARAM),
            _at(day, 7, 43, NARU),
        ]
    today = [_at(4, 7, 0, HOME, "leave_home"), _at(4, 7, 3, BUS)]
    insight = TrainAheadTrigger().check(
        past + today, MONDAY + timedelta(days=4, hours=7, minutes=4)
    )
    # 07:08:10 → 07:09, 07:10:50 → 07:11 (내렸다면 0708:0710)
    assert insight is not None and insight.trigger == f"train_ahead:{GARAM}:{NARU}:0709:0711"


def test_an_odd_morning_does_not_stretch_the_window() -> None:
    """볼일이 있던 하루(80분)가 범위를 07:09~08:23 으로 늘려 16줄 중 14줄이 "빠듯"이었다
    (3차 리뷰)."""
    past = [e for d, ride in enumerate((5, 6, 7, 80)) for e in _bus_then_subway([d], ride=ride)]
    today = [_at(4, 7, 0, HOME, "leave_home"), _at(4, 7, 3, BUS)]
    insight = TrainAheadTrigger().check(
        past + today, MONDAY + timedelta(days=4, hours=7, minutes=4)
    )
    assert insight is not None and insight.trigger == f"train_ahead:{GARAM}:{NARU}:0708:0710"
    assert "평소 5~7분" in insight.summary


# ── 서울 실시간 지하철 ─────────────────────────────────────────────


def test_station_names_from_kakao() -> None:
    station = parse_station("가람역 5호선")
    assert station is not None and (station.base, station.line, station.line_id) == (
        "가람",
        "5호선",
        "1005",
    )
    assert parse_station("나루역 2호선") is not None
    assert parse_station("가나구 다라동") is None  # 동네 이름
    assert parse_station("어느역 9999선") is None


def _arrival(name: str, way: str, heading: str, seconds: int, message: str) -> Dict[str, Any]:
    return {
        "subwayId": "1005",
        "updnLine": way,
        "trainLineNm": heading,
        "statnNm": name,
        "barvlDt": str(seconds),
        "arvlMsg2": message,
        "arvlMsg3": message.split("(")[-1].rstrip(")") if "(" in message else "",
        "recptnDt": "2026-09-11 07:04:00",
    }


def _subway(rows: Dict[str, List[Dict[str, Any]]], asked: List[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        asked.append(name)
        found = rows.get(name) or []
        code = "INFO-000" if found else "INFO-200"
        return httpx.Response(
            200, json={"errorMessage": {"code": code, "message": "-"}, "realtimeArrivalList": found}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


GARAM_ROWS = [
    _arrival("가람(새터)", "하행", "동끝행 - 동녘방면", 120, "2분 후 (서녘)"),
    _arrival("가람(새터)", "하행", "동갈래행 - 동녘방면", 480, "8분 후 (느티)"),
    _arrival("가람(새터)", "상행", "서끝행 - 서녘방면", 240, "4분 후 (솔밭)"),
]


def test_the_api_name_is_found_among_all_stations_when_it_has_a_subtitle() -> None:
    """ "가람"으로 물으면 데이터가 없다 — API 이름은 "가람(새터)"이다(2026-11-11 실측)."""
    asked: List[str] = []
    rows = {"ALL": GARAM_ROWS + [_arrival("동녘", "하행", "동끝행", 60, "1분 후")]}
    client = _subway(rows, asked)
    station = parse_station("가람역 5호선")
    assert station is not None
    assert asyncio.run(api_name(client, "k", station)) == "가람(새터)"
    assert asked == ["가람", "ALL"]
    assert asyncio.run(fetch_arrivals(client, "k", "없는역")) == []  # 데이터 없음은 빈 목록


def test_a_broken_answer_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errorMessage": {"code": "ERROR-337", "message": "한도"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="ERROR-337"):
        asyncio.run(fetch_arrivals(client, "k", "강남"))


def test_arrivals_read_terminus_next_station_and_train_number_and_the_usual_way() -> None:
    arrivals = parse_arrivals(
        {"realtimeArrivalList": [{**r, "btrainNo": "5129"} for r in GARAM_ROWS]}
    )
    assert [a.terminus for a in arrivals] == ["동끝", "동갈래", "서끝"]
    assert [a.toward for a in arrivals] == ["동녘", "동녘", "서녘"]
    assert arrivals[0].train_no == "5129" and arrivals[0].current == "서녘"
    goal, here = (37.45, 127.2), (37.4, 127.1)  # 나루 쪽, 가람역 쪽
    east, west = (37.402, 127.107), (37.405, 127.09)  # 동녘, 서녘
    assert usual_way({"하행": east, "상행": west}, here, goal) == "하행"
    assert usual_way({"하행": None, "상행": west}, here, goal) is None  # 모르면 고르지 않는다


# ── 시간표(서울 열린데이터) ────────────────────────────────────────


def _row(no: str, at: str, dest: str = "동갈래", left: str = "") -> Dict[str, Any]:
    return {"TRAIN_NO": no, "ARRIVETIME": at, "LEFTTIME": left or at, "SUBWAYENAME": dest}


TIMETABLE = [
    _row("5121", "07:06:40", "동끝"),
    _row("5123", "07:09:50"),
    _row("5125", "07:12:30", "동끝"),
    _row("5127", "07:15:20"),
    _row("5129", "07:18:00", "동끝"),
    _row("5131", "07:21:10"),
]


def _open(answers: Dict[str, Any], asked: List[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        service = request.url.path.split("/")[3]
        asked.append(request.url.path)
        rows = answers.get(service)
        if rows is None:
            return httpx.Response(200, json={"RESULT": {"CODE": "INFO-100", "MESSAGE": "키"}})
        head = {"CODE": "INFO-000" if rows else "INFO-200", "MESSAGE": "-"}
        return httpx.Response(200, json={service: {"RESULT": head, "row": rows}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_the_station_code_and_weekday_timetable() -> None:
    """2026-11-11 시험용 키로 본 꼴 그대로 — 가람 1234(05호선), 시간표는 열차번호·도착·행선지."""
    asked: List[str] = []
    stations = [
        {"STATION_CD": "1234", "STATION_NM": "가람", "LINE_NUM": "05호선"},
        {"STATION_CD": "9999", "STATION_NM": "가람", "LINE_NUM": "02호선"},
    ]
    rows = TIMETABLE + [
        _row("5001", "00:00:00", left="05:30:00"),  # 첫 역 — 도착이 비어 출발로
        _row("5999", "24:10:00"),  # 자정 넘은 막차는 안 쓴다
    ]
    client = _open(
        {"SearchInfoBySubwayNameService": stations, "SearchSTNTimeTableByIDService": rows}, asked
    )
    assert line_code("5호선") == "05호선" and line_code("신분당선") == "신분당선"
    assert asyncio.run(station_code(client, "ok", "가람", "5호선")) == "1234"
    # 행 순서에 기대지 않는다 — 노선으로 고른다(3차 리뷰)
    flipped = _open({"SearchInfoBySubwayNameService": stations[::-1]}, [])
    assert asyncio.run(station_code(flipped, "ok", "가람", "5호선")) == "1234"
    assert asyncio.run(station_code(flipped, "ok", "가람", "2호선")) == "9999"
    table = asyncio.run(timetable(client, "ok", "1234", "하행"))
    assert [t.arrives.strftime("%H:%M") for t in table[:3]] == ["05:30", "07:06", "07:09"]
    assert "5999" not in {t.train_no for t in table}
    assert asked[-1].endswith("/1234/1/2/")  # 평일(1), 하행(2)
    asyncio.run(timetable(client, "ok", "1234", "상행"))
    assert asked[-1].endswith("/1234/1/1/")
    broken = _open({}, [])
    with pytest.raises(RuntimeError, match="INFO-100"):
        asyncio.run(station_code(broken, "bad", "가람", "5호선"))


def test_trains_are_tight_or_roomy_by_when_you_reach_the_platform() -> None:
    """실시간 열차번호로 지금 늦음을 세어 시간표에 더하고, 승강장에 닿는 범위로 가른다."""
    day = date(2026, 9, 11)
    table = asyncio.run(
        timetable(_open({"SearchSTNTimeTableByIDService": TIMETABLE}, []), "k", "1", "하행")
    )
    live = [
        ("5123", datetime(2026, 9, 11, 7, 10, 50, tzinfo=KST)),
        ("9999", datetime(2026, 9, 11, 7, 8, tzinfo=KST)),
    ]
    late = lateness(live, table, day)
    assert late == timedelta(minutes=1)  # 5123: 시간표 07:09:50 → 실시간 07:10:50
    assert (
        lateness([("5123", datetime(2026, 9, 11, 9, 0, tzinfo=KST))], table, day) is None
    )  # 다른 회차
    early = datetime(2026, 9, 11, 7, 9, tzinfo=KST)  # 승강장에 빨리 닿는 날
    latest = datetime(2026, 9, 11, 7, 12, tzinfo=KST)  # 늦게 닿는 날
    planned = plan(table, late or timedelta(0), early, latest, day)
    assert [(p.at.strftime("%H:%M"), p.destination, p.tight) for p in planned] == [
        ("07:10", "동갈래", True),  # 07:07:40 동끝행은 못 탄다(뺀다)
        ("07:13", "동끝", False),
        ("07:16", "동갈래", False),
    ]
    assert render_plan(planned).splitlines() == [
        "- 동갈래행 07:10쯤(빠듯 — 빨리 닿는 날만 탄다)",
        "- 동끝행 07:13쯤(여유 있게 탄다)",
        "- 동갈래행 07:16쯤(여유 있게 탄다)",
    ]


def _signal(place: int = GARAM, early: str = "0708", late: str = "0711") -> Insight:
    return Insight(
        trigger=f"train_ahead:{place}:{NARU}:{early}:{late}",
        summary="-",
        severity=Severity.NOTABLE,
        at=datetime(2026, 9, 11, 7, 3, tzinfo=KST),
        family="train",
    )


class Places:
    def __init__(self, places: List[Place]) -> None:
        self.places = places

    def all(self) -> List[Place]:
        return self.places

    def name(self, place_id: int, name: str) -> bool:
        for place in self.places:
            if place.id == place_id and not place.name:
                place.name = name
                return True
        return False


def _place(place_id: int, name: str = "") -> Place:
    seen = datetime(2026, 9, 1, tzinfo=KST)
    return Place(place_id, 37.5 + place_id / 1000, 126.8, 9, seen, seen, name)


STATIONS = {"동녘": (37.402, 127.107), "서녘": (37.405, 127.09)}


def _all(
    rows: Dict[str, List[Dict[str, Any]]], asked: List[str], table: Any = TIMETABLE
) -> httpx.AsyncClient:
    """서울 지하철·카카오 역 찾기·서울 열린데이터 시간표를 한 연결로 — 운영처럼."""
    stations = [{"STATION_CD": "1234", "STATION_NM": "가람", "LINE_NUM": "05호선"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "dapi.kakao.com":
            query = request.url.params["query"]
            asked.append(f"카카오:{query}")
            name = query.split("역")[0]
            spot = STATIONS.get(name)
            docs = [{"place_name": f"{name}역 5호선", "y": spot[0], "x": spot[1]}] if spot else []
            return httpx.Response(200, json={"documents": docs})
        if request.url.host == "openapi.seoul.go.kr":
            service = request.url.path.split("/")[3]
            asked.append(service)
            found = stations if service == "SearchInfoBySubwayNameService" else table
            head = {"CODE": "INFO-000" if found else "INFO-200", "MESSAGE": "-"}
            return httpx.Response(200, json={service: {"RESULT": head, "row": found}})
        name = request.url.path.rsplit("/", 1)[-1]
        asked.append(name)
        found = rows.get(name) or []
        code = "INFO-000" if found else "INFO-200"
        body = {"errorMessage": {"code": code, "message": "-"}, "realtimeArrivalList": found}
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _spot(place_id: int, lat: float, lon: float, name: str = "") -> Place:
    seen = datetime(2026, 9, 1, tzinfo=KST)
    return Place(place_id, lat, lon, 9, seen, seen, name)


LIVE = [
    {
        **_arrival("가람(새터)", "하행", "동갈래행 - 동녘방면", 410, "6분 후 (느티)"),
        "btrainNo": "5123",
    },
    {
        **_arrival("가람(새터)", "상행", "서끝행 - 서녘방면", 240, "4분 후 (솔밭)"),
        "btrainNo": "5120",
    },
]


def _prepare(asked: List[str], table: Any = TIMETABLE) -> TrainArrivals:
    places = Places(
        [
            _spot(GARAM, 37.4, 127.1, "가람역 5호선"),
            _spot(NARU, 37.45, 127.2, "나루역 2호선"),
            _spot(BUS, 37.41, 127.08, "가나구 다라동"),
            _spot(77, 37.5, 126.8),
        ]
    )
    client = _all({"ALL": LIVE, "가람(새터)": LIVE}, asked, table)
    return TrainArrivals(client=client, key="k", kakao_key="kk", open_key="ok", places=places)


def test_the_train_preparation_tells_tight_and_roomy_trains_from_the_timetable() -> None:
    """실시간 두 열차는 늘 역에 닿는 무렵이라 탈 열차를 못 정했다(리뷰 JAR-044 BLOCKED) — 시간표로
    세 번째 열차부터 알고, 실시간 열차번호로 지금 늦음을 더한다(사용자 결정 2026-11-11)."""
    asked: List[str] = []
    prepare = _prepare(asked)
    now = datetime(2026, 9, 11, 7, 4, tzinfo=KST)
    block = asyncio.run(prepare.prepare(_signal(), now))
    assert block is not None
    # 승강장 = 역 범위(07:08~07:11) + 1분. 5123 이 시간표보다 1분 늦게(07:10:50) 다닌다.
    assert block.label.startswith("가람역 5호선 — 평소 타는 방향, 승강장에 닿는 때 07:09~07:12쯤")
    assert "시간표에 지금 1분 늦게 다니는 것을 더함" in block.label
    assert "동끝" not in block.label and "동녘" not in block.label  # 방향 이름을 라벨에 안 넣는다
    assert block.body.splitlines() == [
        "- 동갈래행 07:10쯤(빠듯 — 빨리 닿는 날만 탄다)",
        "- 동끝행 07:13쯤(여유 있게 탄다)",
        "- 동갈래행 07:16쯤(여유 있게 탄다)",
    ]
    assert "카카오:동녘역 5호선" in asked  # 카카오엔 역 이름만
    asyncio.run(prepare.prepare(_signal(), now))
    assert asked.count("SearchSTNTimeTableByIDService") == 1  # 시간표는 하루 한 번
    asyncio.run(prepare.prepare(_signal(), now + timedelta(days=1)))
    assert asked.count("SearchSTNTimeTableByIDService") == 2
    assert asyncio.run(prepare.prepare(_signal(BUS), now)) is None  # 버스 정류장 쪽
    assert asyncio.run(prepare.prepare(_signal(77), now)) is None  # 이름이 아직 없다
    assert asyncio.run(prepare.prepare(_signal(99), now)) is None  # 없는 장소


def test_when_live_trains_do_not_match_the_timetable_it_says_so_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """열차번호 꼴이 달라 못 맞추면 라벨에만 "못 맞춤"이 붙고 아무도 몰랐다(3차 리뷰) — 로그로
    남긴다."""
    odd = [
        {
            **_arrival("가람(새터)", "하행", "동갈래행 - 동녘방면", 410, "6분 후 (느티)"),
            "btrainNo": "K5123",
        },
        {
            **_arrival("가람(새터)", "상행", "서끝행 - 서녘방면", 240, "4분 후 (솔밭)"),
            "btrainNo": "K5120",
        },
    ]
    places = Places(
        [_spot(GARAM, 37.4, 127.1, "가람역 5호선"), _spot(NARU, 37.45, 127.2, "나루역 2호선")]
    )
    prepare = TrainArrivals(
        client=_all({"ALL": odd, "가람(새터)": odd}, []),
        key="k",
        kakao_key="kk",
        open_key="ok",
        places=places,
    )
    with caplog.at_level("INFO", logger="src.sectors.commute.subway"):
        block = asyncio.run(prepare.prepare(_signal(), datetime(2026, 9, 11, 7, 4, tzinfo=KST)))
    assert block is not None and "시간표 그대로(실시간 열차와 못 맞춤)" in block.label
    assert "실시간 1편 중 0편을 시간표와 맞춤" in caplog.text


def test_lateness_is_the_median_of_matched_trains() -> None:
    day = date(2026, 9, 11)
    table = asyncio.run(
        timetable(_open({"SearchSTNTimeTableByIDService": TIMETABLE}, []), "k", "1", "하행")
    )
    live = [
        ("5121", datetime(2026, 9, 11, 7, 7, 40, tzinfo=KST)),  # +1:00
        ("5123", datetime(2026, 9, 11, 7, 12, 50, tzinfo=KST)),  # +3:00
        ("5125", datetime(2026, 9, 11, 7, 14, 30, tzinfo=KST)),  # +2:00
    ]
    assert lateness(live, table, day) == timedelta(minutes=2)


def test_a_station_spot_kakao_did_not_find_is_asked_again_next_time() -> None:
    """못 찾은 것(None)까지 기억하면 그 방향을 그날 내내 모른다(2차 리뷰 Low — 고친 것의 시험)."""
    lost = [
        {
            **_arrival("가람(새터)", "하행", "동갈래행 - 동녘방면", 410, "6분 후 (느티)"),
            "btrainNo": "5123",
        },
        {
            **_arrival("가람(새터)", "상행", "서끝행 - 솔밭방면", 240, "4분 후 (갈대)"),
            "btrainNo": "5120",
        },
    ]
    asked: List[str] = []
    places = Places(
        [_spot(GARAM, 37.4, 127.1, "가람역 5호선"), _spot(NARU, 37.45, 127.2, "나루역 2호선")]
    )
    prepare = TrainArrivals(
        client=_all({"ALL": lost, "가람(새터)": lost}, asked),
        key="k",
        kakao_key="kk",
        open_key="ok",
        places=places,
    )
    now = datetime(2026, 9, 11, 7, 4, tzinfo=KST)
    asyncio.run(prepare.prepare(_signal(), now))
    asyncio.run(prepare.prepare(_signal(), now))
    assert asked.count("카카오:솔밭역 5호선") == 2  # 못 찾은 것은 다시 묻는다
    assert asked.count("카카오:동녘역 5호선") == 1  # 찾은 것은 기억한다


def test_no_train_talk_without_a_timetable_a_roomy_train_or_a_direction() -> None:
    now = datetime(2026, 9, 11, 7, 4, tzinfo=KST)
    assert asyncio.run(_prepare([], table=[]).prepare(_signal(), now)) is None  # 시간표 없음
    short = [_row("5123", "07:09:50")]  # 빠듯한 열차뿐 — 여유 있는 열차를 모른다
    assert asyncio.run(_prepare([], table=short).prepare(_signal(), now)) is None
    places = Places(
        [_spot(GARAM, 37.4, 127.1, "가람역 5호선"), _spot(NARU, 37.4, 127.1, "같은 곳")]
    )
    same = TrainArrivals(
        client=_all({"가람": LIVE}, []), key="k", kakao_key="kk", open_key="ok", places=places
    )
    assert asyncio.run(same.prepare(_signal(), now)) is None  # 방향을 모른다


# ── 카카오로 장소 이름 ─────────────────────────────────────────────


def _kakao(stations: Dict[float, Optional[Dict[str, Any]]], seen: List[str]) -> httpx.AsyncClient:
    """위도 → 그 근처 역(없으면 None). 좌표→주소는 늘 "가나구 다라동"."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "KakaoAK k"
        lat = float(request.url.params.get("y", 0))
        seen.append(f"{request.url.path}@{lat:.3f}")
        if request.url.path.endswith("category.json"):
            found = stations.get(round(lat, 3))
            return httpx.Response(200, json={"documents": [found] if found else []})
        address = {"region_2depth_name": "가나구", "region_3depth_name": "다라동"}
        return httpx.Response(200, json={"documents": [{"address": address}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_kakao_finds_the_nearest_station_or_the_neighborhood_and_fails_loudly() -> None:
    seen: List[str] = []
    client = _kakao({37.505: {"place_name": "가람역 5호선", "distance": "152"}}, seen)
    assert asyncio.run(nearest_station(client, "k", 37.505, 126.8)) == ("가람역 5호선", 152)
    assert asyncio.run(nearest_station(client, "k", 37.504, 126.8)) is None
    assert asyncio.run(region(client, "k", 37.504, 126.8)) == "가나구 다라동"

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"message": "App(jarvis) disabled OPEN_MAP_AND_LOCAL service."}
        )

    broken = httpx.AsyncClient(transport=httpx.MockTransport(refused))
    with pytest.raises(RuntimeError, match="OPEN_MAP_AND_LOCAL"):
        asyncio.run(nearest_station(broken, "k", 37.5, 126.8))


class Events:
    def __init__(self, events: List[Event]) -> None:
        self.events = events

    def recent(self, kind: str, since: datetime) -> List[Event]:
        return [e for e in self.events if e.at >= since]


def test_role_places_get_names_but_home_is_never_sent() -> None:
    seen: List[str] = []
    places = Places([_place(HOME), _place(BUS), _place(GARAM), _place(NARU, "회사 앞 역")])
    stations = {
        round(_place(GARAM).lat, 3): {"place_name": "가람역 5호선", "distance": "152"},
    }
    naming = PlaceNaming(
        client=_kakao(stations, seen),
        key="k",
        events=Events(_bus_then_subway([0, 1, 2, 3])),
        places=places,
    )
    said = asyncio.run(naming.pull(MONDAY + timedelta(days=4)))
    names = {p.id: p.name for p in places.all()}
    assert names == {HOME: "", BUS: "가나구 다라동", GARAM: "가람역 5호선", NARU: "회사 앞 역"}
    assert f"#{GARAM} 아침에 갈아타는 곳 → 가람역 5호선(지하철역 152m)" in said
    assert not any(f"@{_place(HOME).lat:.3f}" in s for s in seen)  # 집 좌표는 안 나간다
    assert asyncio.run(naming.pull(MONDAY + timedelta(days=4))) == "새로 붙일 이름 없음"


def test_the_home_place_is_never_sent_even_when_it_has_another_role() -> None:
    """집 장소가 "아침에 타는 곳"을 겹쳐 가지면 역할 이름만 보고는 집 좌표가 나갔다(리뷰)."""
    seen: List[str] = []
    events = [
        e
        for d in range(4)
        for e in (_at(d, 7, 0, HOME, "leave_home"), _at(d, 7, 2, HOME), _at(d, 7, 40, NARU))
    ]
    places = Places([_place(HOME), _place(NARU)])
    naming = PlaceNaming(client=_kakao({}, seen), key="k", events=Events(events), places=places)
    asyncio.run(naming.pull(MONDAY + timedelta(days=4)))
    assert not any(f"@{_place(HOME).lat:.3f}" in s for s in seen)
    assert {p.id: p.name for p in places.all()}[HOME] == ""
