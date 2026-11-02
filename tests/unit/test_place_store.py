"""장소 저장소 — 사건을 붙이고, 번호는 바뀌지 않는다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.models import Event
from src.storage.events import SQLiteEventStore
from src.storage.places import SQLitePlaceStore

KST = timezone(timedelta(hours=9))
AT = datetime(2026, 10, 2, 7, 0, tzinfo=KST)
HOME = (37.5663, 126.9779)
NEXT_DOOR = (37.5670, 126.9782)  # 약 80m
FAR = (37.4979, 127.0276)


def _event(where: tuple, minutes: int, label: str = "wallet") -> Event:
    return Event(
        source="shortcuts",
        kind="location",
        at=AT + timedelta(minutes=minutes),
        label=label,
        lat=where[0],
        lon=where[1],
    )


def test_가까운_사건은_한_장소로_먼_사건은_새_장소로(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    events, places = SQLiteEventStore(db), SQLitePlaceStore(db)
    events.write([_event(HOME, 0), _event(NEXT_DOOR, 10), _event(FAR, 20)])

    assert places.assign_new() == 3

    found = places.all()
    assert [p.visits for p in found] == [2, 1]
    placed = events.recent("location", AT - timedelta(days=1))
    assert [e.place_id for e in placed] == [found[0].id, found[0].id, found[1].id]


def test_번호는_바뀌지_않고_새로_온_것만_붙인다(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    events, places = SQLiteEventStore(db), SQLitePlaceStore(db)
    events.write([_event(HOME, 0)])
    places.assign_new()
    first = places.all()[0].id

    events.write([_event(NEXT_DOOR, 60)])
    assert places.assign_new() == 1
    assert places.assign_new() == 0
    assert [(p.id, p.visits) for p in places.all()] == [(first, 2)]


def test_좌표_없는_사건은_붙이지_않는다(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    events, places = SQLiteEventStore(db), SQLitePlaceStore(db)
    events.write([Event(source="shortcuts", kind="location", at=AT)])

    assert places.assign_new() == 0
    assert places.all() == []


def test_장소_열이_없던_사건_표에도_붙는다(tmp_path: Path) -> None:
    """JAR-015 로 먼저 만든 사건 표에는 place_id 열이 없다."""
    import sqlite3

    db = tmp_path / "t.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, "
            "kind TEXT NOT NULL, at TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', lat REAL, "
            "lon REAL, meta TEXT NOT NULL DEFAULT '{}', UNIQUE (source, kind, at, label))"
        )
    SQLiteEventStore(db).write([_event(HOME, 0)])

    assert SQLitePlaceStore(db).assign_new() == 1


def test_동시에_붙여도_한_곳이_두_번호가_되지_않는다(tmp_path: Path) -> None:
    """수신구는 요청을 동시에 처리한다. 쓰기 잠금을 먼저 잡지 않으면 같은 사건을 둘이 읽는다."""
    import threading

    db = tmp_path / "t.db"
    events, places = SQLiteEventStore(db), SQLitePlaceStore(db)
    spots = [(37.0 + i * 0.01, 127.0) for i in range(10)]  # 서로 1km 넘게 떨어진 열 곳
    events.write([_event(spots[i % 10], i) for i in range(200)])

    workers = [threading.Thread(target=places.assign_new) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    found = places.all()
    assert len(found) == 10
    assert sum(p.visits for p in found) == 200
