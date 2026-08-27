"""흔적 수신구.

관측치와 다른 점만 본다 — 등록 규율, 중복 무시, 자비스를 안 깨우는 것.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.config import Settings
from src.core.metrics import MetricRegistry
from src.core.traces import TraceRegistry
from src.runtime.ingest import router
from src.sectors.interest import TRACES as INTEREST_TRACES
from src.storage.sqlite import SQLiteStore
from src.storage.traces import SQLiteTraceStore

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    db = tmp_path / "t.db"
    app.state.store = SQLiteStore(db)
    app.state.traces = SQLiteTraceStore(db)
    app.state.metrics = MetricRegistry()
    # 흔적도 지표와 똑같이 섹터가 등록해야 문이 열린다.
    app.state.trace_kinds = TraceRegistry().register(INTEREST_TRACES)
    app.state.settings = Settings(
        brain_base_url="http://localhost:8000",
        brain_model="",
        telegram_bot_token="",
        telegram_chat_id="",
        loop_interval_sec=1800,
        db_path=db,
        ingest_token=TOKEN,
    )
    return TestClient(app)


def _payload(texts: List[str], at: str = "2026-08-27T11:00:00+09:00") -> Dict[str, Any]:
    return {
        "source": "mac_chrome",
        "kind": "web_visit",
        "traces": [
            {"at": at, "text": t, "meta": {"url": f"https://x/{i}"}} for i, t in enumerate(texts)
        ],
    }


def test_흔적을_받아_저장한다(tmp_path: Path) -> None:
    response = _client(tmp_path).post(
        "/ingest/traces", json=_payload(["전세자금대출 금리"]), headers=AUTH
    )
    body = response.json()
    assert body == {"received": 1, "written": 1, "kind": "web_visit"}


def test_같은_흔적을_다시_보내면_무시한다(tmp_path: Path) -> None:
    """수집기가 겹치는 구간을 다시 읽는 건 정상 동작이다.

    크롬 기록을 한 시간마다 최근 세 시간치씩 읽으면 매번 3분의 2가 중복이다.
    """
    client = _client(tmp_path)
    payload = _payload(["전세자금대출 금리", "모니터 추천"])
    first = client.post("/ingest/traces", json=payload, headers=AUTH).json()
    second = client.post("/ingest/traces", json=payload, headers=AUTH).json()
    assert first["written"] == 2
    assert second == {"received": 2, "written": 0, "kind": "web_visit"}


def test_같은_텍스트라도_시각이_다르면_따로_쌓인다(tmp_path: Path) -> None:
    """같은 검색을 두 번 한 건 중복이 아니라 사실이다 — 관심의 강도다."""
    client = _client(tmp_path)
    aim = _payload(["모니터 추천"], at="2026-08-27T11:00:00+09:00")
    later = _payload(["모니터 추천"], at="2026-08-27T15:00:00+09:00")
    client.post("/ingest/traces", json=aim, headers=AUTH)
    body = client.post("/ingest/traces", json=later, headers=AUTH).json()
    assert body["written"] == 1


def test_등록되지_않은_종류는_거절한다(tmp_path: Path) -> None:
    """오타 하나가 조용히 새 종류를 만들면 몇 주 뒤에 풀 수 없게 된다."""
    payload = _payload(["뭔가"])
    payload["kind"] = "web_visits"
    response = _client(tmp_path).post("/ingest/traces", json=payload, headers=AUTH)
    assert response.status_code == 400
    assert "web_visit" in response.json()["detail"]


def test_빈_텍스트는_흔적이_아니다(tmp_path: Path) -> None:
    response = _client(tmp_path).post(
        "/ingest/traces", json=_payload(["", "   ", "진짜"]), headers=AUTH
    )
    body = response.json()
    assert body == {"received": 3, "written": 1, "kind": "web_visit"}


def test_토큰이_없으면_거절한다(tmp_path: Path) -> None:
    assert _client(tmp_path).post("/ingest/traces", json=_payload(["x"])).status_code == 401
