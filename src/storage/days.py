"""공휴일 표 — 받은 공휴일을 남긴다(재시작해도 다시 받기 전까지 쓴다).

관측치와 같은 DB 파일에 산다. 받는 쪽이 해마다 통째로 다시 받아 덮는다 — 지우지 않고 날짜마다
덮어쓴다(대체 공휴일처럼 늦게 생긴 날이 더해진다). 판단은 `src/core/days.py` 가 한다.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterator, Mapping

from src.core.clock import stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS holidays (
    day        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    year       INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


class SQLiteHolidays:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save(self, year: int, days: Mapping[date, str], fetched_at: datetime) -> int:
        """그해의 공휴일을 날짜마다 덮어쓰고, 그해 줄 모두에 받은 때를 적는다."""
        when = stamp(fetched_at)
        rows = [(day.isoformat(), name, year, when) for day, name in days.items()]
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO holidays (day, name, year, fetched_at) VALUES (?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                "UPDATE holidays SET fetched_at = ? WHERE year = ?",
                (when, year),
            )
        return len(rows)

    def load(self) -> Dict[date, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT day, name FROM holidays ORDER BY day").fetchall()
        return {date.fromisoformat(str(day)): str(name) for day, name in rows}

    def fetched(self) -> Dict[int, datetime]:
        """해마다 마지막으로 받은 때."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT year, MAX(fetched_at) FROM holidays GROUP BY year"
            ).fetchall()
        return {int(year): datetime.fromisoformat(str(when)) for year, when in rows}
