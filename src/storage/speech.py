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
    text    TEXT NOT NULL,
    -- 0 이면 "판단했지만 말 안 걸기로 했다". 게이트는 둘 다 보고,
    -- 맥락 제공자는 1 만 본다.
    spoken  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_speech_trigger_at ON speech (trigger, at);
"""


class SQLiteSpeechLog:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # 이미 만들어진 DB에는 spoken 이 없다. 조용히 빼먹으면 게이트가
            # 다시 새므로 여기서 메꾼다.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(speech)")}
            if "spoken" not in columns:
                conn.execute("ALTER TABLE speech ADD COLUMN spoken INTEGER NOT NULL DEFAULT 1")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(self, trigger: str, at: datetime, text: str, spoken: bool = True) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO speech (trigger, at, text, spoken) VALUES (?, ?, ?, ?)",
                (trigger, at.isoformat(), text, 1 if spoken else 0),
            )

    def last(self, trigger: str) -> Optional[SpeechRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT trigger, at, text, spoken FROM speech "
                "WHERE trigger = ? ORDER BY at DESC LIMIT 1",
                (trigger,),
            ).fetchone()
        return _to_record(row) if row else None

    def since(self, moment: datetime) -> List[SpeechRecord]:
        """실제로 한 말만. SKIP 판정이 섞이면 자비스가 하지도 않은 말을
        했다고 착각한다."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trigger, at, text, spoken FROM speech "
                "WHERE at >= ? AND spoken = 1 ORDER BY at",
                (moment.isoformat(),),
            ).fetchall()
        return [_to_record(row) for row in rows]


def _to_record(row: Sequence[object]) -> SpeechRecord:
    # 옛 DB에서 읽은 행은 spoken 칸이 없을 수 있다. 그때는 실제로 한 말로
    # 본다 — SKIP 을 저장하기 전에 쌓인 기록이라 전부 발화였다.
    return SpeechRecord(
        trigger=str(row[0]),
        at=datetime.fromisoformat(str(row[1])),
        text=str(row[2]),
        spoken=bool(row[3]) if len(row) > 3 else True,
    )
