"""자기 점검의 SQLite 구현 — 자비스가 보낸 답을 스스로 돌아본 기록(JAR-039).

관측치·발화 기억과 같은 파일에 산다. 질문과 답은 앞부분만 둔다 — 돌아보기에 필요한 것은 무엇을
물었고 무엇이 막혔나이지 답 전체가 아니다. 지우지 않는다. 읽는 쪽이 기간을 정한다(`/gaps` 는 14일).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Tuple

from src.core.clock import stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS selfchecks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    question TEXT NOT NULL,
    answer   TEXT NOT NULL,
    -- 쓴 도구(종류·대상·됐나·메모)의 JSON 목록.
    tools    TEXT NOT NULL,
    -- 다 함 · 일부 · 못 함
    result   TEXT NOT NULL,
    -- 막힌 갈래(selfcheck.BLOCKED 중 하나)
    blocked  TEXT NOT NULL,
    -- 있었으면 했던 기능(한 줄, 없으면 빈 글)
    missing  TEXT NOT NULL,
    -- 무엇이 모자랐나(한 줄)
    note     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_selfchecks_at ON selfchecks (at);
"""

# 둘 때 자르는 길이(자).
QUESTION = 300
ANSWER = 600
LINE = 200


@dataclass(frozen=True)
class SelfCheck:
    at: datetime
    question: str
    answer: str
    # (종류, 대상, 됐나, 메모)
    tools: Tuple[Tuple[str, str, bool, str], ...]
    result: str
    blocked: str
    missing: str
    note: str


class SQLiteSelfChecks:
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

    def add(self, check: SelfCheck) -> None:
        tools = [[kind, target[:LINE], ok, note[:LINE]] for kind, target, ok, note in check.tools]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO selfchecks (at, question, answer, tools, result, blocked, missing,"
                " note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stamp(check.at),
                    check.question[:QUESTION],
                    check.answer[:ANSWER],
                    json.dumps(tools, ensure_ascii=False),
                    check.result,
                    check.blocked,
                    check.missing[:LINE],
                    check.note[:LINE],
                ),
            )

    def since(self, at: datetime) -> List[SelfCheck]:
        """그때 뒤의 점검(오래된 것부터)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT at, question, answer, tools, result, blocked, missing, note"
                " FROM selfchecks WHERE at >= ? ORDER BY at, id",
                (stamp(at),),
            ).fetchall()
        return [
            SelfCheck(
                at=datetime.fromisoformat(str(row[0])),
                question=str(row[1]),
                answer=str(row[2]),
                tools=tuple(
                    (str(kind), str(target), bool(ok), str(note))
                    for kind, target, ok, note in json.loads(str(row[3]))
                ),
                result=str(row[4]),
                blocked=str(row[5]),
                missing=str(row[6]),
                note=str(row[7]),
            )
            for row in rows
        ]
