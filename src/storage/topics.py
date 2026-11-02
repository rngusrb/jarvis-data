"""주제 나무 저장소.

관측치·흔적과 같은 DB 파일에 산다. 근거가 흔적 **번호**를 가리키므로 같은 파일이어야
잇는 게 성립한다.

다섯 표:
  topics          번호, 부모, 이름, 만든 때, 접은 때. 지우지 않는다 — 접는다
  topic_evidence  주제 ↔ 흔적 연결. 떼면 뗀 때를 적는다. 연결이 바뀌어도 흔적은 그대로다
  topic_changes   모든 변경과 그 이유. 나무가 얼마나 자주 바뀌는지는 여기서 잰다
  notes           LLM 이 자유롭게 적는 메모(옛 믿음의 `value` 자리). 덮어쓰지 않고 쌓는다
  reflections     회고가 돈 기록. 매일 회고가 "오늘 돌았나"를 여기서 본다

나무를 바꾸는 길은 `apply` 하나다. 검사(`core.topics.plan`)가 끝난 나무와 원래 나무를 견줘 그
차이만 쓰고, 한 트랜잭션에 넣는다 — 한 회고의 변경은 전부 적용되거나 하나도 안 된다.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from src.core.clock import stamp
from src.core.topics import Ground, Node, Plan, Tree
from src.storage.traces import SCHEMA as TRACE_SCHEMA

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id  INTEGER,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT
);
CREATE TABLE IF NOT EXISTS topic_evidence (
    topic_id   INTEGER NOT NULL,
    trace_id   INTEGER NOT NULL,
    added_at   TEXT NOT NULL,
    removed_at TEXT
);
-- 같은 연결은 한 번만 살아 있다. 뗐다가 다시 붙이면 새 줄이다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_evidence_live
    ON topic_evidence (topic_id, trace_id) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_topic_evidence_trace ON topic_evidence (trace_id);
CREATE TABLE IF NOT EXISTS topic_changes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    op       TEXT NOT NULL,
    topic_id INTEGER,
    detail   TEXT NOT NULL DEFAULT '{}',
    reason   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    topic_id INTEGER,
    text     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reflections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 회고가 '지금'으로 본 때. 되감기에서는 실제로 돈 때(ran_at)와 다르다.
    at         TEXT NOT NULL,
    ran_at     TEXT NOT NULL,
    considered INTEGER NOT NULL,
    digested   INTEGER NOT NULL,
    applied    INTEGER NOT NULL,
    rejected   INTEGER NOT NULL,
    -- ok · empty(훑을 게 없음) · failed: 이유
    outcome    TEXT NOT NULL
);
"""

# 변경 내용(detail) 안에서 주제 번호인 칸. 새 주제의 임시 번호(음수)를 진짜 번호로 바꿔 적는다.
_TOPIC_KEYS = ("parent", "into", "from", "to")


class StaleTree(Exception):
    """읽어 온 뒤에 다른 회고가 나무를 바꿨다. 낡은 나무 위에서 짠 계획은 적용하지 않는다."""


@dataclass(frozen=True)
class Note:
    topic: Optional[int]
    at: datetime
    text: str


@dataclass(frozen=True)
class ChangeRecord:
    at: datetime
    op: str
    topic: Optional[int]
    detail: Dict[str, Any]
    reason: str


class SQLiteTopicStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # 근거는 흔적을 가리킨다. 흔적 표 없이는 나무를 읽을 수도 없다
            # (근거의 때·글을 거기서 읽는다).
            conn.executescript(TRACE_SCHEMA)
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── 읽기 ──────────────────────────────────────────

    def load(self) -> Tree:
        """접힌 주제까지 전부.

        접힌 주제를 가리키는 연산에 "접힌 주제다"라고 답하려면 알아야 한다.
        """
        with self._connect() as conn:
            version = _version(conn)
            nodes = {
                int(r[0]): Node(
                    id=int(r[0]),
                    name=str(r[2]),
                    parent=int(r[1]) if r[1] is not None else None,
                    created_at=datetime.fromisoformat(str(r[3])),
                    retired_at=datetime.fromisoformat(str(r[4])) if r[4] else None,
                )
                for r in conn.execute(
                    "SELECT id, parent_id, name, created_at, retired_at FROM topics"
                )
            }
            links: Dict[int, set] = {topic: set() for topic in nodes}
            for topic, trace in conn.execute(
                "SELECT topic_id, trace_id FROM topic_evidence WHERE removed_at IS NULL"
            ):
                links.setdefault(int(topic), set()).add(int(trace))
        return Tree(nodes=nodes, links=links, version=version)

    def grounds(self) -> List[Ground]:
        """지금 붙어 있는 근거와 그 흔적의 때·글."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.topic_id, e.trace_id, t.at, t.text FROM topic_evidence e "
                "JOIN traces t ON t.id = e.trace_id WHERE e.removed_at IS NULL"
            ).fetchall()
        return [
            Ground(
                topic=int(r[0]), trace=int(r[1]), at=datetime.fromisoformat(str(r[2])), text=r[3]
            )
            for r in rows
        ]

    def latest_notes(self, per_topic: int) -> Dict[Optional[int], List[Note]]:
        """주제마다 최근 메모 몇 개(최근 것부터). 주제 없는 메모는 `None` 칸에."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT topic_id, at, text FROM ("
                "  SELECT topic_id, at, text, ROW_NUMBER() OVER ("
                "    PARTITION BY topic_id ORDER BY id DESC) AS rank FROM notes"
                ") WHERE rank <= ? ORDER BY at DESC",
                (per_topic,),
            ).fetchall()
        found: Dict[Optional[int], List[Note]] = {}
        for topic, at, text in rows:
            key = int(topic) if topic is not None else None
            found.setdefault(key, []).append(Note(key, datetime.fromisoformat(str(at)), str(text)))
        return found

    def changes(self, since: Optional[datetime] = None) -> List[ChangeRecord]:
        sql = "SELECT at, op, topic_id, detail, reason FROM topic_changes"
        params: List[Any] = []
        if since is not None:
            sql += " WHERE at >= ?"
            params.append(stamp(since))
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY id", params).fetchall()
        return [
            ChangeRecord(
                at=datetime.fromisoformat(str(r[0])),
                op=str(r[1]),
                topic=int(r[2]) if r[2] is not None else None,
                detail=json.loads(r[3]),
                reason=str(r[4]),
            )
            for r in rows
        ]

    def last_run(self) -> Optional[datetime]:
        """마지막으로 **성공한** 회고가 '지금'으로 본 때.

        실패를 돈 것으로 치면 그날은 다시 안 돈다.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(at) FROM reflections WHERE outcome NOT LIKE 'failed%'"
            ).fetchone()
        return datetime.fromisoformat(str(row[0])) if row and row[0] else None

    # ── 쓰기 ──────────────────────────────────────────

    def apply(self, plan: Plan, now: datetime) -> Dict[int, int]:
        """계획을 한 트랜잭션에 적용한다. 새 주제의 임시 번호 → 진짜 번호를 돌려준다."""
        if plan.empty:
            return {}
        conn = sqlite3.connect(self._path, isolation_level=None)
        try:
            # 쓰기 잠금부터 잡는다. 잡은 뒤에 판(version)을 봐야 그 사이에 끼어드는 회고가 없다.
            conn.execute("BEGIN IMMEDIATE")
            current = _version(conn)
            if current != plan.before.version:
                newer = current - plan.before.version
                raise StaleTree(f"나무를 읽은 뒤 변경 {newer}건이 더 적용됐다 — 다시 읽고 짠다")
            ids = _write(conn, plan, now)
            conn.execute("COMMIT")
            return ids
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def record_run(
        self,
        at: datetime,
        ran_at: datetime,
        considered: int,
        digested: int,
        applied: int,
        rejected: int,
        outcome: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reflections "
                "(at, ran_at, considered, digested, applied, rejected, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (stamp(at), stamp(ran_at), considered, digested, applied, rejected, outcome),
            )


def _version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM topic_changes").fetchone()[0])


def _write(conn: sqlite3.Connection, plan: Plan, now: datetime) -> Dict[int, int]:
    before, after = plan.before, plan.after
    ids: Dict[int, int] = {}

    def real(topic: Optional[int]) -> Optional[int]:
        return ids.get(topic, topic) if topic is not None else None

    # 새 주제를 먼저 다 넣고 부모는 나중에 잇는다. 한 응답 안에서 만든 주제를 뒤에 만든 주제
    # 아래로 옮길 수도 있어서, 만든 순서대로 넣으며 부모를 잇다간 아직 없는 번호를 가리킨다.
    fresh = sorted((n for n in after.nodes.values() if n.id < 0), key=lambda n: -n.id)
    for node in fresh:
        cursor = conn.execute(
            "INSERT INTO topics (parent_id, name, created_at, retired_at) VALUES (NULL, ?, ?, ?)",
            (
                node.name,
                stamp(node.created_at),
                stamp(node.retired_at) if node.retired_at else None,
            ),
        )
        ids[node.id] = int(cursor.lastrowid or 0)
    for node in after.nodes.values():
        old = before.nodes.get(node.id)
        if old is not None and (old.name, old.parent, old.retired_at) == (
            node.name,
            node.parent,
            node.retired_at,
        ):
            continue
        conn.execute(
            "UPDATE topics SET name = ?, parent_id = ?, retired_at = ? WHERE id = ?",
            (
                node.name,
                real(node.parent),
                stamp(node.retired_at) if node.retired_at else None,
                real(node.id),
            ),
        )

    for topic in sorted(set(before.links) | set(after.links)):
        was = before.links.get(topic, set())
        now_linked = after.links.get(topic, set())
        for trace in sorted(now_linked - was):
            conn.execute(
                "INSERT INTO topic_evidence (topic_id, trace_id, added_at) VALUES (?, ?, ?)",
                (real(topic), trace, stamp(now)),
            )
        for trace in sorted(was - now_linked):
            conn.execute(
                "UPDATE topic_evidence SET removed_at = ? "
                "WHERE topic_id = ? AND trace_id = ? AND removed_at IS NULL",
                (stamp(now), real(topic), trace),
            )

    for change in plan.changes:
        detail = dict(change.detail)
        for key in _TOPIC_KEYS:
            if isinstance(detail.get(key), int):
                detail[key] = real(detail[key])
        if "children" in detail:
            detail["children"] = [real(k) for k in detail["children"]]
        conn.execute(
            "INSERT INTO topic_changes (at, op, topic_id, detail, reason) VALUES (?, ?, ?, ?, ?)",
            (
                stamp(now),
                change.op,
                real(change.topic),
                json.dumps(detail, ensure_ascii=False),
                change.reason,
            ),
        )
    for note in plan.notes:
        conn.execute(
            "INSERT INTO notes (at, topic_id, text) VALUES (?, ?, ?)",
            (stamp(now), real(note.topic), note.text),
        )
    return ids
