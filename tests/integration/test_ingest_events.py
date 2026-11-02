"""위치는 표본 문으로 들어와 사건 표에 담긴다 — 폰 단축어를 고치지 않는다."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi.testclient import TestClient

from src.core.events import EventRegistry
from src.storage.events import SQLiteEventStore
from src.storage.sqlite import SQLiteStore
from tests.integration.test_ingest_spans import AUTH, _client


def _with_events(tmp_path: Path) -> TestClient:
    from src.sectors.commute.events import EVENTS

    client = _client(tmp_path)
    client.app.state.event_kinds = EventRegistry().register(EVENTS)  # type: ignore[attr-defined]
    client.app.state.events = SQLiteEventStore(tmp_path / "t.db")  # type: ignore[attr-defined]
    return client


# SETUP 2-4 의 이동감지 단축어가 보내는 모양 그대로
LOCATION: Dict[str, Any] = {
    "source": "shortcuts",
    "kind": "location",
    "samples": [
        {
            "at": "2026-10-02T08:17:00+09:00",
            "value": 0,
            "meta": {"lat": 37.5, "lon": 127.0, "trigger": "wallet"},
        }
    ],
}


def test_위치는_사건으로_담긴다(tmp_path: Path) -> None:
    client = _with_events(tmp_path)

    body = client.post("/ingest/samples", json=LOCATION, headers=AUTH).json()

    assert body["written"] == 1
    assert body["received"]["stored_as"] == "events"
    events = SQLiteEventStore(tmp_path / "t.db").recent(
        "location", datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert [(e.label, e.lat) for e in events] == [("wallet", 37.5)]
    # 관측치 표에는 들어가지 않는다 — 들어가면 "위치 0" 이 숫자로 취급된다.
    assert SQLiteStore(tmp_path / "t.db").kinds() == []


def test_다시_보내면_새로_쓰지_않는다(tmp_path: Path) -> None:
    client = _with_events(tmp_path)
    client.post("/ingest/samples", json=LOCATION, headers=AUTH)

    assert client.post("/ingest/samples", json=LOCATION, headers=AUTH).json()["written"] == 0


def test_사건_카드가_없으면_지표로_찾다가_거절한다(tmp_path: Path) -> None:
    # 조용히 아무 데나 담지 않는다. 등록 안 된 종류는 400 이다.
    response = _client(tmp_path).post("/ingest/samples", json=LOCATION, headers=AUTH)
    assert response.status_code == 400
