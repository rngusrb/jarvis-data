"""사건 카드와 점 → 사건 변환."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.events import EventKind, EventRegistry, event_from_point

KST = timezone(timedelta(hours=9))
AT = datetime(2026, 10, 2, 8, 17, tzinfo=KST)


def test_같은_종류를_두_번_등록하면_멈춘다() -> None:
    registry = EventRegistry().register([EventKind(kind="location", label="위치")])
    with pytest.raises(ValueError):
        registry.register([EventKind(kind="location", label="위치")])


def test_단축어가_보낸_점을_사건으로_가른다() -> None:
    # 단축어는 값 칸에 0 을, 좌표와 이유를 meta 에 넣어 보낸다.
    meta = {"lat": "37.5", "lon": 127.0, "trigger": "wallet", "extra": 1}
    event = event_from_point("shortcuts", "location", AT, 0, meta)

    assert (event.label, event.lat, event.lon) == ("wallet", 37.5, 127.0)
    # 0 은 정확도가 아니라 "없음"이다. 남기면 정확도 0m 짜리 점이 된다.
    assert event.meta == {"extra": 1}


def test_정확도는_값이_있을_때만_남긴다() -> None:
    event = event_from_point("shortcuts", "location", AT, 35.0, {"lat": 1, "lon": 2})
    assert event.meta == {"accuracy": 35.0}


def test_좌표나_이유가_없어도_사건이다() -> None:
    event = event_from_point("shortcuts", "location", AT, 0, {"trigger": None})
    assert (event.label, event.lat, event.lon) == ("", None, None)
