"""사건 저장소."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.models import Event
from src.storage.events import SQLiteEventStore

KST = timezone(timedelta(hours=9))
AT = datetime(2026, 10, 2, 8, 17, tzinfo=KST)


def _event(label: str = "wallet", at: datetime = AT) -> Event:
    return Event(source="shortcuts", kind="location", at=at, label=label, lat=37.5, lon=127.0)


def test_다시_보낸_사건은_한_줄이다(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "t.db")
    assert store.write([_event()]) == 1
    # 같은 순간을 UTC 로 다시 보내도 같은 사건이다.
    assert store.write([_event(at=AT.astimezone(timezone.utc))]) == 0


def test_같은_순간이라도_이유가_다르면_다른_사건이다(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "t.db")
    assert store.write([_event("wallet"), _event("leave_home")]) == 2


def test_기간은_순간으로_자르고_좌표와_이유를_돌려준다(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "t.db")
    store.write([_event(at=AT - timedelta(hours=2)), _event("leave_home", at=AT)])

    # 한국 07:00 = UTC 22:00(전날). 06:17 사건은 빠지고 08:17 만 남는다.
    found = store.recent("location", datetime(2026, 10, 2, 7, 0, tzinfo=KST))

    assert [(e.label, e.lat, e.lon) for e in found] == [("leave_home", 37.5, 127.0)]
    assert found[0].at == AT


def test_마지막_수신(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "t.db")
    store.write([_event(at=AT - timedelta(days=1)), _event(at=AT)])
    assert store.last_seen() == {"location": AT}


def test_받은_때는_처음_받은_때로_남는다(tmp_path: Path) -> None:
    """출발·탑승 트리거는 받은 때로 신선도를 잰다. 다시 보낸 줄이 그 때를 미루면 안 된다."""
    store = SQLiteEventStore(tmp_path / "t.db")
    store.write([_event()])
    first = store.recent("location", AT - timedelta(days=1))[0].received_at
    store.write([_event()])

    again = store.recent("location", AT - timedelta(days=1))[0].received_at
    assert first is not None and again == first
