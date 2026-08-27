"""발화 기억의 SQLite 구현.

관측치와 같은 파일에 산다. 별도 DB로 나누면 백업·이전 경로가 둘이 되고,
"자비스가 그때 무슨 말을 했나"를 관측치와 나란히 조회할 수 없다.

관측치 테이블과 달리 중복 제거 키가 없다. 같은 트리거로 같은 시각에 두 번
말하는 건 중복이 아니라 사고이고, 그건 게이트가 막을 일이지 저장소가
덮어써서 감출 일이 아니다.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

from src.brain.memory import SpeechRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS speech (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    at      TEXT NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_speech_trigger_at ON speech (trigger, at);
"""


class SQLiteSpeechLog:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
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

    def record(self, trigger: str, at: datetime, text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO speech (trigger, at, text) VALUES (?, ?, ?)",
                (trigger, at.isoformat(), text),
            )

    def last(self, trigger: str) -> Optional[SpeechRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT trigger, at, text FROM speech WHERE trigger = ? ORDER BY at DESC LIMIT 1",
                (trigger,),
            ).fetchone()
        return _to_record(row) if row else None

    def since(self, moment: datetime) -> List[SpeechRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trigger, at, text FROM speech WHERE at >= ? ORDER BY at",
                (moment.isoformat(),),
            ).fetchall()
        return [_to_record(row) for row in rows]


def _to_record(row: Sequence[object]) -> SpeechRecord:
    return SpeechRecord(
        trigger=str(row[0]), at=datetime.fromisoformat(str(row[1])), text=str(row[2])
    )
