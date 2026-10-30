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
