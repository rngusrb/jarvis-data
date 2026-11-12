"""쉬는 날 — 공휴일 표와 그 저장."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from src.core.clock import KST
from src.core.days import holiday, is_workday, set_holidays
from src.storage.days import SQLiteHolidays

CHUSEOK = {date(2026, 9, 24): "추석", date(2026, 9, 25): "추석", date(2026, 9, 26): "추석"}


def test_holidays_and_weekends_are_days_off() -> None:
    thursday = date(2026, 9, 24)
    assert is_workday(thursday) and holiday(thursday) is None  # 표가 비면 주말만 쉰다
    set_holidays(CHUSEOK)
    assert not is_workday(thursday) and holiday(thursday) == "추석"
    assert not is_workday(date(2026, 9, 27))  # 일요일
    assert is_workday(date(2026, 9, 28))
    set_holidays({})  # 통째로 바꾼다
    assert is_workday(thursday)


def test_the_holiday_store_keeps_years_and_overwrites_days(tmp_path: Path) -> None:
    store = SQLiteHolidays(tmp_path / "t.db")
    first = datetime(2026, 1, 2, 9, 0, tzinfo=KST)
    assert store.load() == {} and store.fetched() == {}
    assert store.save(2026, CHUSEOK, first) == 3
    later = first + timedelta(days=40)
    # 다시 받으면 날짜마다 덮어쓰고, 늦게 생긴 날(대체공휴일)이 더해진다 — 지우지 않는다.
    store.save(2026, {date(2026, 9, 24): "추석", date(2026, 10, 5): "대체공휴일(개천절)"}, later)
    assert store.load() == {**CHUSEOK, date(2026, 10, 5): "대체공휴일(개천절)"}
    assert store.fetched() == {2026: later}
    store.save(2027, {date(2027, 1, 1): "1월1일"}, later)
    assert set(store.fetched()) == {2026, 2027}
