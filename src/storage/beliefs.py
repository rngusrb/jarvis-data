"""믿음 저장소.

관측치·흔적과 같은 DB 파일에 산다.

`kind` 가 기본키다. 같은 종류의 믿음은 하나뿐이어야 한다 — "관심사:주거"가
두 줄이면 어느 게 지금 생각인지 알 수 없다. 새 근거가 나오면 새 줄을 만드는
게 아니라 **기존 줄을 갱신**한다. 그게 흔적(쌓임)과 믿음(갱신됨)의 차이다.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence

from src.core.beliefs import Belief, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS beliefs (
    kind       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    confidence REAL NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    evidence   TEXT NOT NULL DEFAULT '[]',
    status     TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}'
);
"""

# 근거를 무한정 쌓으면 프롬프트가 터진다. 오래된 근거는 이미 확신도에
# 반영됐으므로 최근 것만 남긴다.
MAX_EVIDENCE = 12


class SQLiteBeliefStore:
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

    def get(self, kind: str) -> Optional[Belief]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM beliefs WHERE kind = ?", (kind,)).fetchone()
        return _to_belief(row) if row else None

    def all(self) -> List[Belief]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM beliefs ORDER BY last_seen DESC").fetchall()
        return [_to_belief(r) for r in rows]

    def active(self, now: datetime) -> List[Belief]:
        """프롬프트에 넣어도 되는 믿음.

        후보는 뺀다 — 근거가 한 번뿐인 걸 "이 사람의 관심사"라고 넣으면
        스쳐간 호기심을 붙들고 늘어지게 된다. 시든 것도 뺀다.
        """
        return [b for b in self.all() if b.aged(now) is Status.CONFIRMED]

    def observe(self, belief: Belief, now: datetime) -> Belief:
        """근거를 더한다. 이미 있으면 갱신하고, 없으면 후보로 만든다.

        같은 kind가 두 번째로 관측되는 순간 후보에서 확정으로 올린다.
        이 승격이 이 저장소의 핵심이다 — 한 번짜리와 반복되는 것을 갈라내는
        유일한 자리다.
        """
        existing = self.get(belief.kind)
        if existing is None:
            fresh = Belief(
                kind=belief.kind,
                value=belief.value,
                confidence=belief.confidence,
                first_seen=now,
                last_seen=now,
                evidence=belief.evidence[-MAX_EVIDENCE:],
                status=Status.CANDIDATE,
                meta=belief.meta,
            )
            self._write(fresh)
            return fresh

        # 근거는 합치되 중복은 뺀다. 같은 검색어를 다섯 번 했다고 근거가
        # 다섯 개가 되면 확신도가 부풀려진다.
        merged: List[str] = list(existing.evidence)
        for item in belief.evidence:
            if item not in merged:
                merged.append(item)

        # 승격은 **시간이 지나 근거가 또 나왔다**는 뜻이어야 한다. 같은 회고
        # 안에서 두 번 관측된 걸로 올려주면 한 번의 실행이 스스로를 확정시킨다.
        promoted = Status.CONFIRMED if now > existing.last_seen else existing.status

        updated = Belief(
            kind=existing.kind,
            # 값은 새 관측을 따른다. 관심사는 "이사 알아보는 중"에서
            # "전세 계약 직전"으로 **자라기** 때문이다.
            value=belief.value,
            confidence=max(existing.confidence, belief.confidence),
            first_seen=existing.first_seen,
            last_seen=now,
            evidence=tuple(merged[-MAX_EVIDENCE:]),
            status=promoted,
            meta={**existing.meta, **belief.meta},
        )
        self._write(updated)
        return updated

    def forget_stale(self, now: datetime) -> List[str]:
        """오래 조용한 믿음을 지운다. 지운 kind 목록을 돌려준다.

        생성만 있고 회수가 없으면 몇 달 뒤 3년 전 관심사가 프롬프트에
        끼어든다. **줄이는 쪽이 없으면 이 구조는 죽는다.**
        """
        doomed = [b.kind for b in self.all() if b.forgettable(now)]
        if not doomed:
            return []
        with self._connect() as conn:
            conn.executemany("DELETE FROM beliefs WHERE kind = ?", [(k,) for k in doomed])
        return doomed

    def _write(self, belief: Belief) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO beliefs "
                "(kind, value, confidence, first_seen, last_seen, evidence, status, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    belief.kind,
                    belief.value,
                    belief.confidence,
                    belief.first_seen.isoformat(),
                    belief.last_seen.isoformat(),
                    json.dumps(list(belief.evidence), ensure_ascii=False),
                    belief.status.value,
                    json.dumps(belief.meta, ensure_ascii=False),
                ),
            )


def _to_belief(row: Sequence[Any]) -> Belief:
    return Belief(
        kind=str(row[0]),
        value=str(row[1]),
        confidence=float(row[2]),
        first_seen=datetime.fromisoformat(str(row[3])),
        last_seen=datetime.fromisoformat(str(row[4])),
        evidence=tuple(json.loads(row[5])),
        status=Status(str(row[6])),
        meta=json.loads(row[7]) if row[7] else {},
    )
