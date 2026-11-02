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


def test_좌표가_숫자가_아니면_422_로_돌려보낸다(tmp_path: Path) -> None:
    # 500 이면 맥 배달부가 "나중에 다시"로 보고 그 뒤의 모든 줄을 막는다(JAR-015 리뷰).
    broken = {**LOCATION, "samples": [{**LOCATION["samples"][0], "meta": {"lat": "", "lon": 1}}]}
    response = _with_events(tmp_path).post("/ingest/samples", json=broken, headers=AUTH)
    assert response.status_code == 422


def test_기기_이름은_meta_로_남는다(tmp_path: Path) -> None:
    sample = {**LOCATION["samples"][0], "source": "iPhone"}
    client = _with_events(tmp_path)
    client.post("/ingest/samples", json={**LOCATION, "samples": [sample]}, headers=AUTH)

    event = SQLiteEventStore(tmp_path / "t.db").recent(
        "location", datetime(2026, 1, 1, tzinfo=timezone.utc)
    )[0]
    assert event.meta == {"device": "iPhone"}


def test_새_사건이면_자비스를_깨우고_다시_보낸_것이면_안_깨운다(tmp_path: Path) -> None:
    from tests.integration.test_ingest_spans import _SpyJarvis

    client = _with_events(tmp_path)
    spy = _SpyJarvis()
    client.app.state.jarvis = spy  # type: ignore[attr-defined]

    client.post("/ingest/samples", json=LOCATION, headers=AUTH)
    client.post("/ingest/samples", json=LOCATION, headers=AUTH)

    assert spy.runs == 1


def test_상태_창구에_사건_표의_위치가_보인다(tmp_path: Path) -> None:
    """옮긴 뒤 /health 에서 위치가 사라지면 폰 데이터가 돌아왔는지 사람이 확인할 수 없다."""
    from src.storage.catalog import MergedCatalog

    client = _with_events(tmp_path)
    app_state = client.app.state  # type: ignore[attr-defined]
    app_state.catalog = MergedCatalog(app_state.store, app_state.events)
    client.post("/ingest/samples", json=LOCATION, headers=AUTH)

    body = client.get("/health").json()

    assert "location" in body["kinds"]
    assert body["last_seen"]["location"].startswith("2026-10-01T23:17")
