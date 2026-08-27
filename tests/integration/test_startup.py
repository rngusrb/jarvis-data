"""조립 지점이 실제로 뜨는지.

**이 파일이 없어서 서버가 죽었다.**

2026-08-28. `app/main.py` 가 `sector.TRACES` 를 순회하는데 health 섹터에만
그 속성이 없었다. 서버는 기동 즉시 AttributeError 로 죽고 재시작을 반복했는데,
`harness all` 은 통과했다 — **app/main.py 를 import 하는 테스트가 하나도
없었기 때문이다.** 전부를 무너뜨릴 수 있는 유일한 파일에 커버리지가 0이었다.

여기서 보는 건 로직이 아니라 **조립이 성립하는가**다. 섹터가 늘 때마다
계약을 지켰는지 확인하는 자리이기도 하다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest
from fastapi.testclient import TestClient

from app.main import SECTORS, build_reflector
from src.core.config import Settings

SECTOR_CONTRACT = ("METRICS", "TRACES", "TRIGGERS")


@pytest.mark.parametrize("name", SECTOR_CONTRACT)
def test_every_sector_keeps_the_contract(name: str) -> None:
    """섹터 하나가 모양을 어기면 서버가 통째로 안 뜬다.

    app/main.py 가 특별 취급 없이 순회하는 게 이 구조의 전제다. 빠진 섹터를
    getattr 로 봐주면 그 순간 "어떤 섹터는 뭘 안 낸다"는 예외가 생기고,
    다음 사람은 뭘 내야 하는지 알 수 없게 된다.
    """
    missing = [s.__name__ for s in SECTORS if not hasattr(s, name)]
    assert not missing, f"{name} 가 없는 섹터: {missing}"

    wrong = [s.__name__ for s in SECTORS if not isinstance(getattr(s, name), list)]
    assert not wrong, f"{name} 가 list 가 아닌 섹터: {wrong}"


def test_no_sector_claims_the_same_kind() -> None:
    """두 섹터가 같은 kind를 주장하면 저장소에서 섞인다."""
    for name in ("METRICS", "TRACES"):
        kinds: List[Any] = [c.kind for s in SECTORS for c in getattr(s, name)]
        assert len(kinds) == len(set(kinds)), f"{name} 에 중복 kind: {kinds}"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        brain_base_url="http://localhost:8000",
        brain_model="",
        telegram_bot_token="",
        telegram_chat_id="",
        loop_interval_sec=1800,
        db_path=tmp_path / "t.db",
        ingest_token="test-token",
    )


def test_the_app_actually_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """lifespan 을 끝까지 태운다.

    import 만으로는 부족하다 — 죽은 자리가 lifespan 안이었다. TestClient 를
    컨텍스트로 열어야 startup 이 실제로 돈다.
    """
    monkeypatch.setattr("app.main.load_settings", lambda: _settings(tmp_path))
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_reflector_assembles(tmp_path: Path) -> None:
    """회고 CLI 가 쓰는 조립 경로. 여기도 섹터 모양에 기댄다."""
    reflector = build_reflector(_settings(tmp_path))
    assert [c.kind for c in reflector.kinds] == ["web_visit"]
