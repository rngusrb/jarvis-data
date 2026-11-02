"""장소 저장소 — 좌표가 있는 사건을 가까운 장소에 붙이고, 장소를 남긴다.

사건과 같은 DB 파일에 산다. 붙이는 일은 새 사건이 들어올 때마다 조금씩 한다
(`assign_new`). 처음부터 다시 묶지 않는 이유는 `src/core/places.py` 머리말에 있다 —
장소 번호가 바뀌면 붙여둔 이름이 엉뚱한 곳으로 간다.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, List, Sequence

from src.core.clock import stamp
from src.core.places import SAME_PLACE_METERS, Place, nearest, visit
from src.storage.events import SQLiteEventStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    visits     INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT ''
);
"""


class SQLitePlaceStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        # 장소는 사건에서 나온다. 사건 표(와 그 place_id 열)가 먼저 있어야 한다.
        SQLiteEventStore(self._path)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def all(self) -> List[Place]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, lat, lon, visits, first_seen, last_seen, name FROM places ORDER BY id"
            ).fetchall()
        return [_to_place(r) for r in rows]

    def assign_new(self, radius: float = SAME_PLACE_METERS) -> int:
        """아직 장소가 없는 좌표 사건을 붙인다. 붙인 사건 수를 돌려준다.

        쓰기 잠금을 먼저 잡는다. 수신구는 요청을 동시에 처리하는데, 둘이 같은 사건을 읽고
        각자 새 장소를 만들면 한 곳이 두 번호가 된다.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            places = [
                _to_place(r)
                for r in conn.execute(
                    "SELECT id, lat, lon, visits, first_seen, last_seen, name FROM places"
                )
            ]
            rows = conn.execute(
                "SELECT id, at, lat, lon FROM events "
                "WHERE place_id IS NULL AND lat IS NOT NULL AND lon IS NOT NULL ORDER BY at, id"
            ).fetchall()
            for event_id, at, lat, lon in rows:
                when = datetime.fromisoformat(str(at))
                place = nearest(places, lat, lon, radius)
                if place is None:
                    cursor = conn.execute(
                        "INSERT INTO places (lat, lon, visits, first_seen, last_seen) "
                        "VALUES (?, ?, 1, ?, ?)",
                        (lat, lon, stamp(when), stamp(when)),
                    )
                    place = Place(int(cursor.lastrowid or 0), lat, lon, 1, when, when)
                    places.append(place)
                else:
                    visit(place, lat, lon, when)
                    conn.execute(
                        "UPDATE places SET lat = ?, lon = ?, visits = ?, first_seen = ?, "
                        "last_seen = ? WHERE id = ?",
                        (
                            place.lat,
                            place.lon,
                            place.visits,
                            stamp(place.first_seen),
                            stamp(place.last_seen),
                            place.id,
                        ),
                    )
                conn.execute("UPDATE events SET place_id = ? WHERE id = ?", (place.id, event_id))
            return len(rows)


def _to_place(row: Sequence[Any]) -> Place:
    return Place(
        id=int(row[0]),
        lat=float(row[1]),
        lon=float(row[2]),
        visits=int(row[3]),
        first_seen=datetime.fromisoformat(str(row[4])),
        last_seen=datetime.fromisoformat(str(row[5])),
        name=str(row[6]),
    )
