"""스스로 알아본 것의 SQLite 구현 — 탐색 루프가 남긴다(JAR-046).

관측치·발화 기억과 같은 파일에 산다. 한 줄이 한 번의 탐색이다: 무엇을(씨앗의 갈래·표·주제) 알아봤고,
무엇을 알게 됐고(요약·사실·출처), 알릴 만한지. 지우지 않는다 — 읽는 쪽이 기간을 정한다. 실패(시간
초과·검색 없이 씀 등)도 까닭과 함께 남긴다 — 같은 씨앗을 시계마다 되풀이하지 않게.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from src.core.clock import stamp
from src.core.findings import Fact, Finding

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    -- 씨앗의 갈래와 표(같은 씨앗을 다시 뽑기까지 센다)
    kind    TEXT NOT NULL,
    key     TEXT NOT NULL,
    subject TEXT NOT NULL,
    -- 알게 된 것(날짜 말 없이), 사실들(JSON — 글·절대 날짜·출처), 출처 주소·검색어(JSON 목록)
    summary TEXT NOT NULL,
    facts   TEXT NOT NULL,
    sources TEXT NOT NULL,
    queries TEXT NOT NULL,
    -- 알릴 것 · 기록만 · 없음
    worth   TEXT NOT NULL,
    -- 있으면 좋을 기능(없으면 빈 글)
    idea    TEXT NOT NULL DEFAULT '',
    -- 제대로 판단하지 못한 까닭(비면 제대로 된 판단)
    note    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_findings_at ON findings (at);
CREATE INDEX IF NOT EXISTS idx_findings_key ON findings (key, at);
"""

# 둘 때 자르는 길이(자)와 개수.
SUBJECT = 300
SUMMARY = 800
FACT = 300
QUOTE = 200
FACTS = 8
SOURCES = 10
QUERIES = 10
URL = 500
IDEA = 300
NOTE = 200


class SQLiteFindings:
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

    def write(self, finding: Finding) -> int:
        facts = [
            {
                "text": f.text[:FACT],
                "day": f.day.isoformat() if f.day else None,
                "source": f.source[:URL],
                "quote": f.quote[:QUOTE],
                "no_year": f.no_year,
            }
            for f in finding.facts[:FACTS]
        ]
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO findings (at, kind, key, subject, summary, facts, sources, queries,"
                " worth, idea, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stamp(finding.at),
                    finding.kind,
                    finding.key,
                    finding.subject[:SUBJECT],
                    finding.summary[:SUMMARY],
                    json.dumps(facts, ensure_ascii=False),
                    json.dumps([s[:URL] for s in finding.sources[:SOURCES]], ensure_ascii=False),
                    json.dumps(list(finding.queries[:QUERIES]), ensure_ascii=False),
                    finding.worth,
                    finding.idea[:IDEA],
                    finding.note[:NOTE],
                ),
            )
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)

    def since(self, at: datetime) -> List[Finding]:
        """그때부터 알아본 것, 먼저 것부터."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, at, kind, key, subject, summary, facts, sources, queries, worth, idea,"
                " note FROM findings WHERE at >= ? ORDER BY at, id",
                (stamp(at),),
            ).fetchall()
        return [_to_finding(r) for r in rows]

    def last_at(self) -> Optional[datetime]:
        """마지막으로 알아본 때 — 재시작하자마자 또 돌지 않게 시계가 본다."""
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(at) FROM findings").fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def explored(self, since: datetime) -> Dict[str, datetime]:
        """그때부터 씨앗 표마다 마지막으로 알아본 때."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, MAX(at) FROM findings WHERE at >= ? GROUP BY key", (stamp(since),)
            ).fetchall()
        return {key: datetime.fromisoformat(at) for key, at in rows}

    def tries(self, key: str) -> int:
        """그 씨앗을 제대로 알아본 횟수(실패는 안 센다 — 한 번뿐인 기회를 원인 없이 쓰지 않게)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE key = ? AND note = ''", (key,)
            ).fetchone()
        return int(row[0])

    def queries_since(self, at: datetime) -> List[str]:
        """그때부터 쓴 검색어(되풀이하지 않고, 하루 검색 수를 센다)."""
        out: List[str] = []
        with self._connect() as conn:
            for (raw,) in conn.execute(
                "SELECT queries FROM findings WHERE at >= ? ORDER BY at", (stamp(at),)
            ):
                out.extend(json.loads(raw))
        return out


def _to_finding(row: Tuple) -> Finding:
    fid, at, kind, key, subject, summary, facts, sources, queries, worth, idea, note = row
    return Finding(
        at=datetime.fromisoformat(at),
        kind=kind,
        key=key,
        subject=subject,
        summary=summary,
        facts=tuple(
            Fact(
                text=f.get("text", ""),
                day=date.fromisoformat(f["day"]) if f.get("day") else None,
                source=f.get("source", ""),
                quote=f.get("quote", ""),
                no_year=bool(f.get("no_year", False)),
            )
            for f in json.loads(facts)
        ),
        sources=tuple(json.loads(sources)),
        queries=tuple(json.loads(queries)),
        worth=worth,
        idea=idea,
        note=note,
        id=int(fid),
    )
