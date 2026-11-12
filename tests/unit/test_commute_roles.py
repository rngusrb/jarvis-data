"""장소의 역할 후보 — 집, 아침에 타는 곳, 내리는 곳, 저녁에 타는 곳."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from src.core.models import Event
from src.sectors.commute.places import roles

KST = timezone(timedelta(hours=9))
MONDAY = datetime(2026, 9, 7, tzinfo=KST)  # 월요일
HOME, STATION, WORK_STATION, LUNCH, TRANSFER = 1, 2, 3, 4, 5


def _at(day: int, hour: int, minute: int, place: int, label: str = "wallet") -> Event:
    when = MONDAY + timedelta(days=day, hours=hour, minutes=minute)
    return Event(source="s", kind="location", at=when, label=label, place_id=place)


def _week(days: List[int]) -> List[Event]:
    out: List[Event] = []
    for d in days:
        out += [
            _at(d, 7, 0, HOME, "leave_home"),
            _at(d, 7, 12, STATION),  # 아침 첫 탭 — 타는 곳
            _at(d, 7, 30, TRANSFER),  # 사이 탭 — 갈아타는 곳
            _at(d, 8, 5, WORK_STATION),  # 아침 마지막 탭 — 내리는 곳
            _at(d, 12, 10, LUNCH),
            _at(d, 17, 50, WORK_STATION),  # 저녁 첫 탭 — 타는 곳
            _at(d, 18, 40, STATION),
        ]
    return out


def test_출근_루틴에서_역할을_찾는다() -> None:
    found = {r.name: (r.place_id, r.days, r.of) for r in roles(_week([0, 1, 2, 3, 4]))}

    assert found == {
        "집": (HOME, 5, 5),
        "아침에 타는 곳": (STATION, 5, 5),
        "아침에 갈아타는 곳": (TRANSFER, 5, 5),
        "아침에 내리는 곳": (WORK_STATION, 5, 5),
        "저녁에 타는 곳": (WORK_STATION, 5, 5),
    }


def test_주말은_세지_않는다() -> None:
    # 토·일만 있으면 평일 루틴이 아니다.
    assert roles(_week([5, 6])) == []


def test_사흘이_안_되면_우연이다() -> None:
    assert roles(_week([0, 1])) == []


def test_시각은_한국_시각으로_센다() -> None:
    # UTC 22:12 는 한국 다음 날 07:12 — 아침 탭이다.
    utc = timezone.utc
    taps = [
        Event(
            source="s",
            kind="location",
            at=datetime(2026, 9, 6 + d, 22, 12, tzinfo=utc),
            label="wallet",
            place_id=STATION,
        )
        for d in range(3)
    ]
    assert [r.name for r in roles(taps)] == ["아침에 타는 곳"]


def test_근거는_탭_수가_아니라_날_수다() -> None:
    # 환승역은 내리고 타며 두 번, 지오펜스는 들락날락하며 두 번 찍힌다. 이틀은 이틀이다.
    days: List[Event] = []
    for d in (0, 1):
        days += [
            _at(d, 7, 0, HOME, "leave_home"),
            _at(d, 7, 2, HOME, "leave_home"),
            _at(d, 7, 12, STATION),
            _at(d, 7, 39, TRANSFER),
            _at(d, 7, 40, TRANSFER),
            _at(d, 8, 5, WORK_STATION),
        ]
    found = {r.name: r for r in roles(days)}

    assert "집" not in found and "아침에 갈아타는 곳" not in found


def test_공휴일도_세지_않고_집_좌표를_꺼낸다() -> None:
    """추석 사흘의 아침은 평일이어도 출근 루틴이 아니다 — 평소에 안 넣는다(JAR-043). 날씨는 그렇게
    찾은 집의 좌표로 예보를 받는다."""
    from datetime import date

    from src.core.days import set_holidays
    from src.core.places import Place
    from src.sectors.commute.places import home_now, home_spot

    week = _week([0, 1, 2])
    assert {r.name for r in roles(week)} >= {"집"}
    set_holidays({date(2026, 9, 7): "임시", date(2026, 9, 8): "임시", date(2026, 9, 9): "임시"})
    assert roles(week) == []
    set_holidays({})
    seen = MONDAY
    home = Place(id=HOME, lat=37.5, lon=127.0, visits=3, first_seen=seen, last_seen=seen)
    assert home_spot(week, [home]) == (37.5, 127.0)
    assert home_spot(_week([0, 1]), [home]) is None  # 집을 아직 모른다(사흘 미만)
    assert home_spot(week, []) is None  # 장소 표에 없으면

    class Events:
        def recent(self, kind: str, since: datetime) -> List[Event]:
            assert kind == "location"
            return [e for e in week if e.at >= since]

    class Places:
        def all(self) -> List[Place]:
            return [home]

    assert home_now(Events(), Places(), MONDAY + timedelta(days=10)) == (37.5, 127.0)
    assert (
        home_now(Events(), Places(), MONDAY + timedelta(days=60)) is None
    )  # 42일보다 오래된 것은 안 본다
