"""대화 저장소 — 주고받은 말과 폴링 커서.

발화 기억(`speech.py`)과 왜 따로 두나: 거기는 **트리거가 낳은 발화**를
담는다. `(trigger, at, spoken)` 이 모양이고, 게이트가 쿨다운을 재는 데 쓴다.
대화는 트리거가 없다. 사용자가 먼저 꺼낸 말에는 붙일 트리거가 없고, 거기에
가짜 트리거 이름을 붙이면 쿨다운 계산이 오염된다.

커서를 같이 두는 이유는 둘이 한 몸이기 때문이다. "어디까지 읽었나"와
"무슨 말을 주고받았나"가 어긋나면 같은 메시지를 두 번 처리하거나 통째로
건너뛴다.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    who   TEXT NOT NULL,          -- 'user' | 'jarvis'
    at    TEXT NOT NULL,
    text  TEXT NOT NULL,
    meta  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_messages_at ON messages (at);

CREATE TABLE IF NOT EXISTS cursors (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Message:
    who: str
    at: datetime
    text: str
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def from_user(self) -> bool:
        return self.who == "user"


class SQLiteConversation:
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

    # ── 커서 ────────────────────────────────────────

    def cursor(self, name: str = "telegram") -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM cursors WHERE name = ?", (name,)).fetchone()
        return int(row[0]) if row else 0

    def advance(self, name: str, value: int) -> None:
        """커서를 앞으로만 민다.

        뒤로 가면 이미 처리한 메시지를 다시 읽는다. 응답이 뒤섞여 도착해도
        안전하도록 저장소에서 막는다.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO cursors (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value "
                "WHERE excluded.value > cursors.value",
                (name, value),
            )

    # ── 대화 ────────────────────────────────────────

    def add(self, who: str, at: datetime, text: str, meta: Dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (who, at, text, meta) VALUES (?, ?, ?, ?)",
                (who, at.isoformat(), text, json.dumps(meta or {}, ensure_ascii=False)),
            )

    def recent(self, limit: int = 20) -> List[Message]:
        """최근 주고받은 말. **오래된 것부터** 돌려준다 — 프롬프트에 넣을 순서다.

        `at` 이 아니라 삽입 순서로 정렬한다. 두 시각이 서로 다른 시계에서
        온다 — 사용자 메시지는 텔레그램이 찍은 시각이고 자비스 답장은 우리
        시계다. 한쪽이 조금만 틀어져도 **대화 순서가 뒤집힌다.** 주고받은
        순서는 id 가 더 정확하고, 그건 틀어질 수가 없다.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT who, at, text, meta FROM messages ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_to_message(row) for row in reversed(rows)]

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])


def _to_message(row: Sequence[Any]) -> Message:
    return Message(
        who=str(row[0]),
        at=datetime.fromisoformat(str(row[1])),
        text=str(row[2]),
        meta=json.loads(row[3]) if row[3] else {},
    )
