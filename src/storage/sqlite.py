"""관측치 저장소 — SQLite.

Qdrant가 아니라 SQLite인 이유: 관측치는 시계열 숫자고, 필요한 질의가
"최근 8일치 가져와" 같은 범위 조회다. 벡터 DB는 의미 유사도를 찾는 물건이라
이런 걸 아주 못한다. Qdrant는 나중에 텍스트(메모, 대화, 관심사)용으로 쓴다.

**멱등성이 이 파일의 핵심 요구사항이다.** 수집 파이프라인은 언제든 재실행될 수
있어야 한다 — 단축어가 두 번 울렸든, export.xml을 다시 넣었든, 같은 날짜의
같은 지표는 한 줄로 남아야 한다. (source, kind, at)을 기본키로 잡은 이유다.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Sequence

from src.core.models import Observation

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    source TEXT NOT NULL,
    kind   TEXT NOT NULL,
    at     TEXT NOT NULL,
    value  REAL NOT NULL,
    meta   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source, kind, at)
);
CREATE INDEX IF NOT EXISTS idx_observations_kind_at ON observations(kind, at);
"""


class SQLiteStore:
    """ObservationSource 구현 + 쓰기.

    연결을 들고 있지 않고 호출마다 연다. sqlite3 연결은 스레드를 넘나들면
    터지는데, FastAPI 이벤트 루프와 배치 스크립트가 같은 저장소를 건드리기
    때문이다. 개인용 규모에서 이 정도 오버헤드는 무시해도 된다.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def write(self, observations: Iterable[Observation]) -> int:
        """관측치를 저장한다. 같은 (source, kind, at)이 이미 있으면 덮어쓴다."""
        rows = [
            (
                observation.source,
                observation.kind,
                observation.at.isoformat(),
                observation.value,
                json.dumps(observation.meta, ensure_ascii=False),
            )
            for observation in observations
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO observations (source, kind, at, value, meta) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def recent(self, kind: str, since: datetime) -> Sequence[Observation]:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT source, kind, at, value, meta FROM observations "
                "WHERE kind = ? AND at >= ? ORDER BY at",
                (kind, since.isoformat()),
            )
            rows = cursor.fetchall()
        return [_to_observation(row) for row in rows]

    def kinds(self) -> List[str]:
        with self._connect() as conn:
            cursor = conn.execute("SELECT DISTINCT kind FROM observations ORDER BY kind")
            return [row[0] for row in cursor.fetchall()]

    def count(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM observations")
            total: int = cursor.fetchone()[0]
            return total


def _to_observation(row: Sequence[Any]) -> Observation:
    source, kind, at, value, meta = row
    parsed: Dict[str, Any] = json.loads(meta)
    return Observation(
        source=source,
        kind=kind,
        value=value,
        at=datetime.fromisoformat(at),
        meta=parsed,
    )
