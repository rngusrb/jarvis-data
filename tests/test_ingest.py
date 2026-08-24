from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.config import Settings
from src.core.metrics import MetricRegistry
from src.runtime.ingest import router
from src.sectors.health import METRICS as HEALTH_METRICS
from src.storage.sqlite import SQLiteStore

TOKEN = "test-token"

PAYLOAD: Dict[str, Any] = {
    "source": "shortcuts",
    "observations": [
        {"kind": "sleep_hours", "value": 7.2, "at": "2026-08-19T00:00:00+09:00"},
        {"kind": "step_count", "value": 8123, "at": "2026-08-19T00:00:00+09:00"},
    ],
}


def _client(tmp_path: Path, token: str = TOKEN) -> TestClient:
    # lifespan을 타지 않는 최소 앱 — 테스트가 자비스 루프를 깨우면 안 된다.
    app = FastAPI()
    app.include_router(router)
    app.state.store = SQLiteStore(tmp_path / "t.db")
    # 섹터가 자기 카드를 등록하는 것이 수신구가 도는 전제다.
    app.state.metrics = MetricRegistry().register(HEALTH_METRICS)
    app.state.settings = Settings(
        brain_base_url="http://localhost:8000",
        brain_model="",
        telegram_bot_token="",
        telegram_chat_id="",
        loop_interval_sec=1800,
        db_path=tmp_path / "t.db",
        ingest_token=token,
    )
    return TestClient(app)


def test_토큰이_맞으면_저장된다(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post("/ingest", json=PAYLOAD, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json()["written"] == 2


def test_토큰이_없으면_거부한다(tmp_path: Path) -> None:
    response = _client(tmp_path).post("/ingest", json=PAYLOAD)
    assert response.status_code == 401


def test_토큰이_틀리면_거부한다(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post("/ingest", json=PAYLOAD, headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_서버에_토큰_설정이_없으면_아예_안_받는다(tmp_path: Path) -> None:
    """인증 없이 열려 있는 것보다 대놓고 막히는 편이 낫다."""
    client = _client(tmp_path, token="")
    response = client.post("/ingest", json=PAYLOAD, headers={"Authorization": "Bearer anything"})
    assert response.status_code == 503


def test_두_번_보내도_중복되지_않는다(tmp_path: Path) -> None:
    """단축어가 두 번 울렸을 때."""
    client = _client(tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    client.post("/ingest", json=PAYLOAD, headers=headers)
    client.post("/ingest", json=PAYLOAD, headers=headers)
    assert client.get("/health").json()["observations"] == 2


def test_이상한_값은_거절한다(tmp_path: Path) -> None:
    client = _client(tmp_path)
    bad = {"observations": [{"kind": "sleep_hours", "value": "일곱시간", "at": "2026-08-19"}]}
    response = client.post("/ingest", json=bad, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 422
