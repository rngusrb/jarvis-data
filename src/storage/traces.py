"""흔적 저장소.

관측치와 같은 DB 파일에 산다. 나누면 백업 경로가 둘이 되고, "그날 뭘
검색했고 얼마나 잤나"를 한 번에 볼 수 없다.

관측치와 결정적으로 다른 게 중복 규칙이다. 관측치는 `(source, kind, at)`
하나당 한 줄이다 — 같은 날 수면이 두 줄일 수는 없으니까. 흔적은 아니다.
같은 URL을 1초 안에 두 번 열 수도 있고, 그건 사고가 아니라 사실이다.

그래서 `text` 까지 포함해 중복을 판단하고, 겹치면 **조용히 무시**한다.
수집기가 겹치는 구간을 다시 읽는 게 정상 동작이기 때문이다 — 크롬 기록을
한 시간마다 최근 세 시간치씩 읽으면 매번 3분의 2가 중복이다.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from src.core.models import Trace

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind   TEXT NOT NULL,
    at     TEXT NOT NULL,
    text   TEXT NOT NULL,
    meta   TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, kind, at, text)
);
CREATE INDEX IF NOT EXISTS idx_traces_kind_at ON traces (kind, at);
"""


class SQLiteTraceStore:
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

    def write(self, traces: Iterable[Trace]) -> int:
        """새로 저장된 줄 수를 돌려준다. 중복은 세지 않는다.

        받은 개수가 아니라 **새로 들어간 개수**를 돌려주는 게 중요하다.
        수집기가 "300건 보냄"이라고 로그를 남기는데 실제로는 3건만 새 것이면,
        그 차이가 보여야 수집기가 제대로 도는지 알 수 있다.
        """
        rows = [
            (t.source, t.kind, t.at.isoformat(), t.text, json.dumps(t.meta, ensure_ascii=False))
            for t in traces
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO traces (source, kind, at, text, meta) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            return conn.total_changes - before

    def recent(self, kind: str, since: datetime, limit: Optional[int] = None) -> Sequence[Trace]:
        """`limit` 은 최신 것부터 자른다 — 오래된 걸 남기고 새 걸 버리면 안 된다."""
        sql = "SELECT source, kind, at, text, meta FROM traces WHERE kind = ? AND at >= ?"
        params: List[Any] = [kind, since.isoformat()]
        if limit is not None:
            sql += " ORDER BY at DESC LIMIT ?"
            params.append(limit)
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [_to_trace(r) for r in reversed(rows)]
        sql += " ORDER BY at"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_to_trace(r) for r in rows]

    def kinds(self) -> List[str]:
        with self._connect() as conn:
            return [r[0] for r in conn.execute("SELECT DISTINCT kind FROM traces ORDER BY kind")]

    def last_seen(self) -> Dict[str, datetime]:
        with self._connect() as conn:
            rows = conn.execute("SELECT kind, MAX(at) FROM traces GROUP BY kind").fetchall()
        return {str(k): datetime.fromisoformat(str(v)) for k, v in rows if v}

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0])


def _to_trace(row: Sequence[Any]) -> Trace:
    return Trace(
        source=str(row[0]),
        kind=str(row[1]),
        at=datetime.fromisoformat(str(row[2])),
        text=str(row[3]),
        meta=json.loads(row[4]) if row[4] else {},
    )
