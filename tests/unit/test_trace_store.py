"""흔적 저장소."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.models import Trace
from src.storage.traces import SQLiteTraceStore

NOW = datetime(2026, 8, 27, 11, 0, tzinfo=timezone.utc)


def _trace(text: str, minutes_ago: int = 0, kind: str = "web_visit") -> Trace:
    return Trace(
        source="mac_chrome",
        kind=kind,
        text=text,
        at=NOW - timedelta(minutes=minutes_ago),
        meta={"url": "https://example.com"},
    )


def test_write_returns_newly_stored_not_received(tmp_path: Path) -> None:
    """받은 수가 아니라 새로 들어간 수를 돌려줘야 수집기 상태가 보인다."""
    store = SQLiteTraceStore(tmp_path / "t.db")
    assert store.write([_trace("a"), _trace("b", 1)]) == 2
    assert store.write([_trace("a"), _trace("c", 2)]) == 1


def test_recent_limit_keeps_the_newest(tmp_path: Path) -> None:
    """오래된 걸 남기고 새 걸 버리면 프롬프트에 옛날 얘기만 들어간다."""
    store = SQLiteTraceStore(tmp_path / "t.db")
    store.write([_trace(f"검색 {i}", minutes_ago=i) for i in range(10)])
    found = store.recent("web_visit", NOW - timedelta(hours=1), limit=3)
    assert [t.text for t in found] == ["검색 2", "검색 1", "검색 0"]


def test_recent_returns_time_order(tmp_path: Path) -> None:
    store = SQLiteTraceStore(tmp_path / "t.db")
    store.write([_trace("나중", 0), _trace("먼저", 5)])
    assert [t.text for t in store.recent("web_visit", NOW - timedelta(hours=1))] == ["먼저", "나중"]


def test_meta_survives_the_round_trip(tmp_path: Path) -> None:
    store = SQLiteTraceStore(tmp_path / "t.db")
    store.write([_trace("한글 검색어")])
    found = store.recent("web_visit", NOW - timedelta(hours=1))[0]
    assert found.text == "한글 검색어"
    assert found.meta["url"] == "https://example.com"


def test_kinds_and_last_seen(tmp_path: Path) -> None:
    store = SQLiteTraceStore(tmp_path / "t.db")
    store.write([_trace("웹"), _trace("파일", 3, kind="file_seen")])
    assert store.kinds() == ["file_seen", "web_visit"]
    assert store.last_seen()["web_visit"] == NOW
    assert store.count() == 2
