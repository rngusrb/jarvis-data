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


def test_두_기기가_같은_구간을_세면_한_번만_센다(tmp_path: Path) -> None:
    """워치와 아이폰이 같은 걸음을 각자 기록한다. 더하면 8천이 1만 5천이 된다."""
    payload = {
        "kind": "step_count",
        "samples": [
            {
                "at": "2026-08-24T09:00:00+09:00",
                "end": "2026-08-24T10:00:00+09:00",
                "value": 1000,
                "source": "구현규의 Apple Watch",
            },
            {
                "at": "2026-08-24T09:00:00+09:00",
                "end": "2026-08-24T10:00:00+09:00",
                "value": 900,
                "source": "구현규의 iPhone",
            },
        ],
    }
    body = _client(tmp_path).post("/ingest/samples", json=payload, headers=AUTH).json()
    assert body["observations"][0]["value"] == 1000.0


def test_워치가_비운_구간은_아이폰이_채운다(tmp_path: Path) -> None:
    """워치 배터리가 죽은 오후까지 버리면 과소 집계가 된다."""
    payload = {
        "kind": "step_count",
        "samples": [
            {
                "at": "2026-08-24T09:00:00+09:00",
                "end": "2026-08-24T10:00:00+09:00",
                "value": 1000,
                "source": "구현규의 Apple Watch",
            },
            {
                "at": "2026-08-24T14:00:00+09:00",
                "end": "2026-08-24T15:00:00+09:00",
                "value": 700,
                "source": "구현규의 iPhone",
            },
        ],
    }
    body = _client(tmp_path).post("/ingest/samples", json=payload, headers=AUTH).json()
    assert body["observations"][0]["value"] == 1700.0


def test_절반만_겹치면_절반만_인정한다(tmp_path: Path) -> None:
    payload = {
        "kind": "step_count",
        "samples": [
            {
                "at": "2026-08-24T09:00:00+09:00",
                "end": "2026-08-24T10:00:00+09:00",
                "value": 1000,
                "source": "구현규의 Apple Watch",
            },
            {
                "at": "2026-08-24T09:30:00+09:00",
                "end": "2026-08-24T10:30:00+09:00",
                "value": 800,
                "source": "구현규의 iPhone",
            },
        ],
    }
    body = _client(tmp_path).post("/ingest/samples", json=payload, headers=AUTH).json()
    assert body["observations"][0]["value"] == 1400.0


def test_소스가_하나뿐이면_그대로_더한다(tmp_path: Path) -> None:
    """소스 정보 없이 오는 경로도 있다. 중복이 없는데 걷어내면 데이터가 사라진다."""
    body = (
        _client(tmp_path).post("/ingest/samples", json=_payload("step_count"), headers=AUTH).json()
    )
    values = {o["at"][:10]: o["value"] for o in body["observations"]}
    assert values["2026-08-24"] == 200.0


def test_받은_원본의_윤곽을_돌려준다(tmp_path: Path) -> None:
    body = (
        _client(tmp_path).post("/ingest/samples", json=_payload("step_count"), headers=AUTH).json()
    )
    assert body["received"]["samples"] == 3
    assert body["received"]["max"] == 120.0
