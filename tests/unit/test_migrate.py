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
    path = tmp_path / "t.db"
    monkeypatch.setenv("JARVIS_DB_PATH", str(path))
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
    assert store.recent("location", EPOCH) == []
    # 다른 지표는 건드리지 않는다.
    assert [o.value for o in store.recent("sleep_hours", EPOCH)] == [6.5]


def test_다시_돌려도_안전하다(db: Path) -> None:
    from app.migrate import main

    _seed(db)
    main(["--apply"])

    assert main(["--apply"]) == 0
    assert SQLiteEventStore(db).count() == 2
