"""사건 저장소.

관측치·흔적과 같은 DB 파일에 산다. 백업 경로를 하나로 두려는 것이다.

중복 판단은 `(source, kind, at, label)` 이다. 같은 순간에 "집을 나섬"과 "카드 탭"이
함께 찍힐 수는 있어도, 같은 순간 같은 이유로 두 번 찍힌 건 같은 사건이다 — 단축어가
두 번 울렸거나 배달부가 다시 보낸 것이다. 겹치면 조용히 무시한다(멱등).

시각은 쓸 때 `stamp()` 로 UTC 를 맞춘다. 그래야 글자 비교가 곧 순간 비교다.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Sequence

from src.core.clock import stamp
from src.core.models import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind   TEXT NOT NULL,
    at     TEXT NOT NULL,
    label  TEXT NOT NULL DEFAULT '',
    lat    REAL,
    lon    REAL,
    meta   TEXT NOT NULL DEFAULT '{}',
    place_id INTEGER,
    UNIQUE (source, kind, at, label)
);
CREATE INDEX IF NOT EXISTS idx_events_kind_at ON events (kind, at);
"""


class SQLiteEventStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # 장소 번호 열은 표가 생긴 뒤에 붙었다(JAR-016). 먼저 만든 표에는 더한다.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            if "place_id" not in columns:
                conn.execute("ALTER TABLE events ADD COLUMN place_id INTEGER")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def write(self, events: Iterable[Event]) -> int:
        """새로 저장된 줄 수를 돌려준다. 다시 보낸 것은 세지 않는다."""
        rows = [
            (
                e.source,
                e.kind,
                stamp(e.at),
                e.label,
                e.lat,
                e.lon,
                json.dumps(e.meta, ensure_ascii=False),
            )
            for e in events
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO events (source, kind, at, label, lat, lon, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            return conn.total_changes - before

    def recent(self, kind: str, since: datetime) -> List[Event]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source, kind, at, label, lat, lon, meta, place_id FROM events "
                "WHERE kind = ? AND at >= ? ORDER BY at, id",
                (kind, stamp(since)),
            ).fetchall()
        return [_to_event(r) for r in rows]

    def last_seen(self) -> Dict[str, datetime]:
        with self._connect() as conn:
            rows = conn.execute("SELECT kind, MAX(at) FROM events GROUP BY kind").fetchall()
        return {str(k): datetime.fromisoformat(str(v)) for k, v in rows if v}

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])


def _to_event(row: Sequence[Any]) -> Event:
    return Event(
        source=str(row[0]),
        kind=str(row[1]),
        at=datetime.fromisoformat(str(row[2])),
        label=str(row[3]),
        lat=row[4],
        lon=row[5],
        meta=json.loads(row[6]) if row[6] else {},
        place_id=row[7],
    )
