from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ingest import router
from src.core.config import Settings
from src.parsers.health import iter_records, nightly_sleep
from src.storage.sqlite import SQLiteStore
from tests.test_health_parser import FIXTURE

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# test_health_parser의 픽스처와 같은 밤이다. 워치 23:00~03:00, 아이폰 01:00~07:00.
SPANS: Dict[str, Any] = {
    "spans": [
        {
            "start": "2026-08-18T23:00:00+09:00",
            "end": "2026-08-19T03:00:00+09:00",
            "stage": "AsleepCore",
        },
        {
            "start": "2026-08-19T01:00:00+09:00",
            "end": "2026-08-19T07:00:00+09:00",
            "stage": "HKCategoryValueSleepAnalysisAsleepUnspecified",
        },
        {
            "start": "2026-08-18T22:00:00+09:00",
            "end": "2026-08-18T23:00:00+09:00",
            "stage": "InBed",
        },
        {
            "start": "2026-08-19T03:00:00+09:00",
            "end": "2026-08-19T03:30:00+09:00",
            "stage": "Awake",
        },
    ]
}


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.store = SQLiteStore(tmp_path / "t.db")
    app.state.settings = Settings(
        brain_base_url="http://localhost:8000",
        brain_model="",
        telegram_bot_token="",
        telegram_chat_id="",
        loop_interval_sec=1800,
        db_path=tmp_path / "t.db",
        ingest_token=TOKEN,
    )
    return TestClient(app)


def test_구간을_받아_밤으로_집계한다(tmp_path: Path) -> None:
    response = _client(tmp_path).post("/ingest/spans", json=SPANS, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["written"] == 1
    assert body["observations"][0]["value"] == 8.0


def test_조각_수가_보존된다(tmp_path: Path) -> None:
    """이게 이 엔드포인트의 존재 이유다 — 아이폰이 합산해 보내면 사라지는 정보."""
    response = _client(tmp_path).post("/ingest/spans", json=SPANS, headers=AUTH)
    assert response.json()["observations"][0]["segments"] == 2


def test_누워만_있거나_깬_구간은_빠진다(tmp_path: Path) -> None:
    response = _client(tmp_path).post("/ingest/spans", json=SPANS, headers=AUTH)
    # InBed 1시간과 Awake 30분이 섞였다면 8.0을 넘었을 것이다.
    assert response.json()["observations"][0]["value"] == 8.0


def test_백필과_단축어가_같은_값을_낸다(tmp_path: Path) -> None:
    """계산이 한 곳에만 있는지 확인하는 테스트.

    두 경로가 갈라지면 "왜 이 날만 값이 튀지"를 추적하기 지독히 어려워진다.
    """
    export = tmp_path / "export.xml"
    export.write_text(FIXTURE, encoding="utf-8")
    from_backfill = nightly_sleep(list(iter_records(export)))[0]

    body = _client(tmp_path).post("/ingest/spans", json=SPANS, headers=AUTH).json()
    from_shortcut = body["observations"][0]

    assert from_shortcut["value"] == from_backfill.value
    assert from_shortcut["segments"] == from_backfill.meta["segments"]


def test_단계_정보가_없으면_전부_수면으로_본다(tmp_path: Path) -> None:
    payload = {
        "spans": [{"start": "2026-08-18T23:00:00+09:00", "end": "2026-08-19T04:00:00+09:00"}]
    }
    body = _client(tmp_path).post("/ingest/spans", json=payload, headers=AUTH).json()
    assert body["observations"][0]["value"] == 5.0


def test_수면이_하나도_없으면_아무것도_안_쓴다(tmp_path: Path) -> None:
    payload = {
        "spans": [
            {
                "start": "2026-08-18T22:00:00+09:00",
                "end": "2026-08-18T23:00:00+09:00",
                "stage": "InBed",
            }
        ]
    }
    body = _client(tmp_path).post("/ingest/spans", json=payload, headers=AUTH).json()
    assert body["written"] == 0


def test_지원하지_않는_종류는_거절한다(tmp_path: Path) -> None:
    payload = {"kind": "step_count", "spans": []}
    response = _client(tmp_path).post("/ingest/spans", json=payload, headers=AUTH)
    assert response.status_code == 400


def test_토큰이_없으면_거부한다(tmp_path: Path) -> None:
    assert _client(tmp_path).post("/ingest/spans", json=SPANS).status_code == 401


def test_두_경로가_같은_밤을_중복시키지_않는다(tmp_path: Path) -> None:
    """source가 양쪽 다 apple_health여야 한다. 다르면 같은 밤이 두 줄로 남는다."""
    client = _client(tmp_path)
    client.post("/ingest/spans", json=SPANS, headers=AUTH)
    client.post(
        "/ingest",
        json={
            "observations": [
                {"kind": "sleep_hours", "value": 8.0, "at": "2026-08-19T00:00:00+09:00"}
            ]
        },
        headers=AUTH,
    )
    assert client.get("/health").json()["observations"] == 1


def test_마지막_수신_시각을_알려준다(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/ingest/spans", json=SPANS, headers=AUTH)
    last_seen = client.get("/health").json()["last_seen"]
    assert "sleep_hours" in last_seen


def test_한국어_단계도_알아본다(tmp_path: Path) -> None:
    """단축어는 한국어로 "수면 시간" / "깨어 있는 시간" 두 가지만 준다.

    실제 아이폰에서 받은 값이다 — Core/Deep/REM 구분 없이 뭉뚱그려 온다.
    """
    payload = {
        "spans": [
            {
                "start": "2026-08-24T04:28:47+09:00",
                "end": "2026-08-24T07:28:47+09:00",
                "stage": "수면 시간",
            },
            {
                "start": "2026-08-24T07:28:47+09:00",
                "end": "2026-08-24T08:28:47+09:00",
                "stage": "깨어 있는 시간",
            },
        ]
    }
    body = _client(tmp_path).post("/ingest/spans", json=payload, headers=AUTH).json()
    # 깨어 있던 1시간이 섞였다면 4.0이 나왔을 것이다.
    assert body["observations"][0]["value"] == 3.0


def test_모르는_표현은_수면으로_본다(tmp_path: Path) -> None:
    """수면을 가리키는 말은 출처마다 다르다. 모른다고 버리면 데이터가 조용히 사라진다."""
    payload = {
        "spans": [
            {
                "start": "2026-08-24T04:00:00+09:00",
                "end": "2026-08-24T06:00:00+09:00",
                "stage": "Schlafenszeit",
            }
        ]
    }
    body = _client(tmp_path).post("/ingest/spans", json=payload, headers=AUTH).json()
    assert body["observations"][0]["value"] == 2.0


def test_영어_한국어_모두_깨어있음을_걸러낸다(tmp_path: Path) -> None:
    for stage in ("Awake", "HKCategoryValueSleepAnalysisAwake", "깨어 있는 시간", "In Bed"):
        payload = {
            "spans": [
                {
                    "start": "2026-08-24T04:00:00+09:00",
                    "end": "2026-08-24T06:00:00+09:00",
                    "stage": stage,
                }
            ]
        }
        body = _client(tmp_path).post("/ingest/spans", json=payload, headers=AUTH).json()
        assert body["written"] == 0, f"{stage}가 수면으로 잘못 통과됨"


@dataclass
class _SpyJarvis:
    runs: int = 0

    async def run_once(self) -> int:
        self.runs += 1
        return 0


def test_수집되면_자비스를_바로_깨운다(tmp_path: Path) -> None:
    """30분 주기를 기다리지 않는다 — 기상 직후의 말과 30분 뒤의 말은 쓸모가 다르다."""
    client = _client(tmp_path)
    spy = _SpyJarvis()
    client.app.state.jarvis = spy  # type: ignore[attr-defined]

    client.post("/ingest/spans", json=SPANS, headers=AUTH)
    assert spy.runs == 1


def test_저장할_게_없으면_깨우지_않는다(tmp_path: Path) -> None:
    client = _client(tmp_path)
    spy = _SpyJarvis()
    client.app.state.jarvis = spy  # type: ignore[attr-defined]

    payload = {
        "spans": [
            {
                "start": "2026-08-18T22:00:00+09:00",
                "end": "2026-08-18T23:00:00+09:00",
                "stage": "깨어 있는 시간",
            }
        ]
    }
    client.post("/ingest/spans", json=payload, headers=AUTH)
    assert spy.runs == 0
