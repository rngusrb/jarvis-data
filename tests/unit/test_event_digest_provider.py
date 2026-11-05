"""최근 사건을 날마다 한 줄로 — 사건 이름의 뜻은 카드가 들고 온다(플랫폼은 섹터를 모른다)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Sequence

from src.brain.providers import EventDigestProvider
from src.core.clock import KST
from src.core.events import EventKind
from src.core.models import Event

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)  # 한국 12:00
CARD = EventKind(
    kind="location",
    label="위치",
    collector="단축어",
    labels=(("leave_home", "집을 나섬"), ("wallet", "지갑 탭")),
)


class Events:
    def __init__(self, found: List[Event]) -> None:
        self.found = found

    def recent(self, kind: str, since: datetime) -> Sequence[Event]:
        return [e for e in self.found if e.kind == kind and e.at >= since]


def _event(label: str, hour: int, minute: int, day: int = 5, place: int = 3) -> Event:
    at = datetime(2026, 11, day, hour, minute, tzinfo=KST)
    return Event(source="phone", kind="location", at=at, label=label, place_id=place)


def test_events_become_one_line_per_day_in_korean_time() -> None:
    found = [
        _event("leave_home", 7, 40),
        _event("wallet", 7, 43, place=5),
        _event("wallet", 18, 25, day=4, place=6),
        _event("wallet", 7, 30, day=1),  # 창(2일) 밖
        _event("wallet", 13, 0),  # 지금(12:00) 뒤
    ]
    block = EventDigestProvider(events=Events(found), kinds=[CARD]).fetch(None, NOW)
    assert block is not None and block.label == "최근 2일 사건"
    assert block.body.splitlines() == [
        "- 위치 11-04(수): 18:25 지갑 탭(장소 #6)",
        "- 위치 11-05(목): 07:40 집을 나섬(장소 #3) · 07:43 지갑 탭(장소 #5)",
    ]


def test_an_unknown_event_name_is_shown_as_is() -> None:
    """카드가 모르는 이름을 조용히 빼면 그런 사건이 없었던 것처럼 보인다."""
    assert CARD.describe("arrive_work") == "arrive_work"
    block = EventDigestProvider(events=Events([_event("arrive_work", 8, 15)]), kinds=[CARD]).fetch(
        None, NOW
    )
    assert block is not None and "08:15 arrive_work(장소 #3)" in block.body


def test_nothing_to_show_is_no_block() -> None:
    assert EventDigestProvider(events=Events([]), kinds=[CARD]).fetch(None, NOW) is None
    retired = EventKind(kind="location", label="위치")
    found = [_event("wallet", 7, 43)]
    assert EventDigestProvider(events=Events(found), kinds=[retired]).fetch(None, NOW) is None
