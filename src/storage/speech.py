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
from src.core.clock import stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS speech (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    at      TEXT NOT NULL,
    text    TEXT NOT NULL,
    -- 0 이면 "판단했지만 말 안 걸기로 했다". 게이트는 둘 다 보고,
    -- 맥락 제공자는 1 만 본다.
    spoken  INTEGER NOT NULL DEFAULT 1,
    -- 무엇에 대한 판단이었나(신호의 at). 게이트가 같은 밤을 두 번 묻지 않으려고 본다.
    about   TEXT,
    -- 먼저 건 말에 붙인 짧은 표(👍👎 버튼이 들고 온다)와 사용자의 반응(1·-1·0), 누른 때.
    token    TEXT,
    feedback INTEGER NOT NULL DEFAULT 0,
    rated_at TEXT,
    -- 큰 줄기(건강·이동·관심사·수집). 게이트가 "이 줄기로 마지막에 말한 때"를 이걸로 찾는다.
    family   TEXT
);
CREATE INDEX IF NOT EXISTS idx_speech_trigger_at ON speech (trigger, at);
"""


class SQLiteSpeechLog:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # 이미 만들어진 DB에는 spoken·about 이 없다. 조용히 빼먹으면 게이트가
            # 다시 새므로 여기서 메꾼다.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(speech)")}
            for column in (
                "spoken INTEGER NOT NULL DEFAULT 1",
                "about TEXT",
                "token TEXT",
                "feedback INTEGER NOT NULL DEFAULT 0",
                "rated_at TEXT",
                "family TEXT",
            ):
                if column.split()[0] not in columns:
                    conn.execute(f"ALTER TABLE speech ADD COLUMN {column}")
            if "family" not in columns:
                # 줄기 칸이 생기기 전 기록 중 이름에 줄기가 든 것(`topic:12`)만 채운다. 안 채우면
                # 배포 직전에 한 관심사 얘기를 게이트가 못 봐서 줄기 간격(6시간) 안에 또 말한다.
                # 이름에 줄기가 없는 옛 기록(sleep_drop 등)은 비워 둔다 — 저장소는 섹터를 모른다.
                conn.execute(
                    "UPDATE speech SET family = substr(trigger, 1, instr(trigger, ':') - 1)"
                    " WHERE family IS NULL AND instr(trigger, ':') > 0"
                )
            # 표는 ALTER 뒤에야 칸이 있다. 이 색인은 버튼이 돌아왔을 때 그 말을 찾는다.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_speech_token ON speech (token)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(
        self,
        trigger: str,
        at: datetime,
        text: str,
        spoken: bool = True,
        about: Optional[datetime] = None,
        token: Optional[str] = None,
        family: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO speech (trigger, at, text, spoken, about, token, family)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    trigger,
                    stamp(at),
                    text,
                    1 if spoken else 0,
                    stamp(about) if about else None,
                    token,
                    family or None,
                ),
            )

    def last(self, trigger: str) -> Optional[SpeechRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM speech WHERE trigger = ? ORDER BY at DESC LIMIT 1",
                (trigger,),
            ).fetchone()
        return _to_record(row) if row else None

    def judged_since(self, trigger: str, about: datetime) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM speech WHERE trigger = ? AND (about = ? OR at > ?) LIMIT 1",
                (trigger, stamp(about), stamp(about)),
            ).fetchone()
        return row is not None

    def last_spoken_in(self, family: str) -> Optional[SpeechRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM speech WHERE family = ? AND spoken = 1"
                " ORDER BY at DESC LIMIT 1",
                (family,),
            ).fetchone()
        return _to_record(row) if row else None

    def since(self, moment: datetime) -> List[SpeechRecord]:
        """실제로 한 말만. SKIP 판정이 섞이면 자비스가 하지도 않은 말을
        했다고 착각한다."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM speech WHERE at >= ? AND spoken = 1 ORDER BY at",
                (stamp(moment),),
            ).fetchall()
        return [_to_record(row) for row in rows]

    def rated(self, moment: datetime) -> List[SpeechRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM speech WHERE rated_at >= ? AND spoken = 1"
                " AND feedback != 0 ORDER BY rated_at",
                (stamp(moment),),
            ).fetchall()
        return [_to_record(row) for row in rows]

    def rate(self, token: str, value: int, at: datetime) -> Optional[SpeechRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM speech WHERE token = ? AND spoken = 1 ORDER BY id DESC LIMIT 1",
                (token,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE speech SET feedback = ?, rated_at = ? WHERE id = ?",
                (value, stamp(at), row[0]),
            )
            found = conn.execute(
                f"SELECT {_COLUMNS} FROM speech WHERE id = ?", (row[0],)
            ).fetchone()
        return _to_record(found)


_COLUMNS = "trigger, at, text, spoken, about, token, feedback, rated_at, family"


def _to_record(row: Sequence[object]) -> SpeechRecord:
    return SpeechRecord(
        trigger=str(row[0]),
        at=datetime.fromisoformat(str(row[1])),
        text=str(row[2]),
        spoken=bool(row[3]),
        about=datetime.fromisoformat(str(row[4])) if row[4] else None,
        token=str(row[5]) if row[5] else None,
        feedback=int(str(row[6] or 0)),
        rated_at=datetime.fromisoformat(str(row[7])) if row[7] else None,
        family=str(row[8]) if row[8] else "",
    )
