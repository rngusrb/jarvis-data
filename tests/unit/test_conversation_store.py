"""대화 저장소."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.storage.conversation import SQLiteConversation


def _indexes(db: Path) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [row[1] for row in conn.execute("PRAGMA index_list(messages)")]


def test_쓰지_않는_시각_인덱스를_걷는다(tmp_path: Path) -> None:
    # 이미 돌던 서버의 DB 에는 옛 인덱스가 남아 있다. 새로 만들 때만 빼면 거기선 안 걷힌다.
    db = tmp_path / "t.db"
    SQLiteConversation(db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE INDEX idx_messages_at ON messages (at)")

    SQLiteConversation(db)

    assert "idx_messages_at" not in _indexes(db)
