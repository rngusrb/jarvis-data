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
    labels=(("leave_home", "집을 나섬"), ("wallet", "지갑 탭")),  # 시험용 뜻(실제 뜻은 섹터 카드)
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
    assert block is not None and block.label.startswith("최근 2일 사건(날은 자정에 바뀐다")
    assert block.body.splitlines() == [
        "- 위치 11-04(수): 18:25 지갑 탭(장소 #6)",
        "- 위치 11-05(목): 07:40 집을 나섬(장소 #3) · 07:43 지갑 탭(장소 #5)",
    ]


def test_days_are_whole_and_empty_days_say_so() -> None:
    """시각으로 창을 자르면 첫날이 잘린 채 온전한 하루처럼 보였다.

    빈 날은 줄이 아예 없었다(리뷰 재현).
    """
    morning = datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)  # 한국 09:00
    found = [_event("leave_home", 7, 30, day=3), _event("wallet", 18, 20, day=3)]
    block = EventDigestProvider(events=Events(found), kinds=[CARD], days=3).fetch(None, morning)
    assert block is not None
    assert block.body.splitlines() == [
        "- 위치 11-03(화): 07:30 집을 나섬(장소 #3) · 18:20 지갑 탭(장소 #3)",
        "- 위치 11-04(수): 기록 없음",
        "- 위치 11-05(목): 기록 없음",
    ]


def test_an_event_without_a_name_reads_as_its_kind() -> None:
    """2026-10 전 옛 사건은 이름이 비어 있다(실데이터 9건)."""
    assert CARD.describe("") == "위치"


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


def test_named_places_read_with_their_names() -> None:
    """ "장소 #5"로는 어디인지 모른다 — 카카오로 붙인 이름을 같이(JAR-044)."""
    from src.core.places import Place

    class Places:
        def all(self) -> List[Place]:
            seen = datetime(2026, 11, 1, tzinfo=KST)
            return [
                Place(5, 37.5, 126.8, 9, seen, seen, "가람역 5호선"),
                Place(3, 37.5, 126.8, 9, seen, seen),
            ]

    events = Events([_event("wallet", 7, 9, place=5), _event("leave_home", 7, 0, place=3)])
    block = EventDigestProvider(events=events, kinds=[CARD], places=Places()).fetch(None, NOW)
    assert block is not None
    assert "(장소 #5 가람역 5호선)" in block.body and "(장소 #3)" in block.body
