"""보여줄 시각."""

from __future__ import annotations

from datetime import datetime, timezone

from src.core.clock import KST, local


def test_UTC_를_한국_시각으로_바꾼다() -> None:
    shown = local(datetime(2026, 10, 30, 3, 56, tzinfo=timezone.utc))
    assert (shown.day, shown.hour, shown.minute) == (30, 12, 56)
    assert shown.utcoffset() == KST.utcoffset(None)


def test_시간대_없는_값은_UTC_로_본다() -> None:
    assert local(datetime(2026, 10, 30, 15, 0)).day == 31


def test_지금은_한국_시간에_요일까지_보여준다() -> None:
    from src.core.clock import shown

    # UTC 로 일요일 밤 15:55 는 한국 월요일 00:55 다.
    assert shown(datetime(2026, 11, 1, 15, 55, tzinfo=timezone.utc)) == "2026-11-02(월) 00:55"
