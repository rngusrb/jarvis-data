"""위치를 관측치 표에서 사건 표로 옮기기."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.models import Observation
from src.storage.events import SQLiteEventStore
from src.storage.sqlite import SQLiteStore

KST = timezone(timedelta(hours=9))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _seed(db: Path) -> SQLiteStore:
    store = SQLiteStore(db)
    store.write(
        [
            Observation(
                source="shortcuts",
                kind="location",
                value=0.0,
                at=datetime(2026, 10, 2, 8, 17, tzinfo=KST),
                meta={"lat": 37.5, "lon": 127.0, "trigger": "wallet"},
            ),
            Observation(
                source="shortcuts",
                kind="location",
                value=0.0,
                at=datetime(2026, 10, 2, 7, 42, tzinfo=KST),
                meta={"lat": 37.4, "lon": 127.1, "trigger": "leave_home"},
            ),
            Observation(
                source="apple_health",
                kind="sleep_hours",
                value=6.5,
                at=datetime(2026, 10, 2, tzinfo=KST),
            ),
        ]
    )
    return store


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from src.core.config import Settings

    path = tmp_path / "t.db"
    # 설정을 바꿔 끼운다. load_settings 를 그대로 부르면 개발자의 실제 .env(봇 토큰 포함)가
    # 시험 프로세스에 실린다(JAR-015 리뷰).
    settings = Settings(
        brain_base_url="http://localhost:8000",
        brain_model="",
        telegram_bot_token="",
        telegram_chat_id="",
        loop_interval_sec=1800,
        db_path=path,
        ingest_token="",
    )
    monkeypatch.setattr("app.migrate.load_settings", lambda: settings)
    return path


def test_시험_실행은_아무것도_바꾸지_않는다(db: Path) -> None:
    from app.migrate import main

    store = _seed(db)
    assert main([]) == 0

    assert len(store.recent("location", EPOCH)) == 2
    assert SQLiteEventStore(db).count() == 0


def test_옮기고_관측치는_지운다(db: Path) -> None:
    from app.migrate import main

    store = _seed(db)
    assert main(["--apply"]) == 0

    events = SQLiteEventStore(db).recent("location", EPOCH)
    assert [(e.label, e.lat) for e in events] == [("leave_home", 37.4), ("wallet", 37.5)]
    # 옮긴 사건도 장소에 붙는다.
    assert all(e.place_id is not None for e in events)
    assert store.recent("location", EPOCH) == []
    # 다른 지표는 건드리지 않는다.
    assert [o.value for o in store.recent("sleep_hours", EPOCH)] == [6.5]


def test_다시_돌려도_안전하다(db: Path) -> None:
    from app.migrate import main

    _seed(db)
    main(["--apply"])

    assert main(["--apply"]) == 0
    assert SQLiteEventStore(db).count() == 2


def test_사건_표에_안_들어갔으면_지우지_않는다(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.migrate import main

    store = _seed(db)
    monkeypatch.setattr(SQLiteEventStore, "write", lambda self, events: 0)

    assert main(["--apply"]) == 1
    assert len(store.recent("location", EPOCH)) == 2


def test_옮길_수_없는_줄이_있으면_아무것도_바꾸지_않는다(db: Path) -> None:
    from app.migrate import main

    store = _seed(db)
    store.write(
        [
            Observation(
                source="shortcuts",
                kind="location",
                value=0.0,
                at=datetime(2026, 10, 3, 8, 0, tzinfo=KST),
                meta={"lat": "", "lon": 127.0},
            )
        ]
    )

    assert main(["--apply"]) == 1
    assert len(store.recent("location", EPOCH)) == 3
    assert SQLiteEventStore(db).count() == 0
