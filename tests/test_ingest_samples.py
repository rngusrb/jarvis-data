from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from tests.test_ingest_spans import AUTH, _client


def _payload(kind: str) -> Dict[str, Any]:
    return {
        "kind": kind,
        "samples": [
            {"at": "2026-08-24T09:00:00+09:00", "value": 120},
            {"at": "2026-08-24T10:00:00+09:00", "value": 80},
            {"at": "2026-08-23T10:00:00+09:00", "value": 50},
        ],
    }


def test_걸음수는_하루치를_더한다(tmp_path: Path) -> None:
    body = (
        _client(tmp_path).post("/ingest/samples", json=_payload("step_count"), headers=AUTH).json()
    )
    assert body["written"] == 2
    values = {o["at"][:10]: o["value"] for o in body["observations"]}
    assert values["2026-08-24"] == 200.0
    assert values["2026-08-23"] == 50.0


def test_심박은_하루치를_평균낸다(tmp_path: Path) -> None:
    body = (
        _client(tmp_path)
        .post("/ingest/samples", json=_payload("heart_rate_avg"), headers=AUTH)
        .json()
    )
    values = {o["at"][:10]: o["value"] for o in body["observations"]}
    assert values["2026-08-24"] == 100.0
    assert values["2026-08-24"] != 200.0


def test_집계법을_모르는_종류는_거절한다(tmp_path: Path) -> None:
    """조용히 아무 방식으로나 접는 것보다 대놓고 막히는 편이 낫다."""
    response = _client(tmp_path).post("/ingest/samples", json=_payload("혈중산소"), headers=AUTH)
    assert response.status_code == 400
    assert "혈중산소" in response.json()["detail"]


def test_토큰이_없으면_거부한다(tmp_path: Path) -> None:
    assert _client(tmp_path).post("/ingest/samples", json=_payload("step_count")).status_code == 401


def test_두_번_보내도_중복되지_않는다(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/ingest/samples", json=_payload("step_count"), headers=AUTH)
    client.post("/ingest/samples", json=_payload("step_count"), headers=AUTH)
    assert client.get("/health").json()["observations"] == 2
