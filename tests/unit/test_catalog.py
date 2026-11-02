"""여러 저장소의 마지막 수신을 한 장으로."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

import pytest

from src.storage.catalog import MergedCatalog

AT = datetime(2026, 11, 2, tzinfo=timezone.utc)


class Fake:
    def __init__(self, seen: Dict[str, datetime]) -> None:
        self.seen = seen

    def last_seen(self) -> Dict[str, datetime]:
        return self.seen


def test_합친다() -> None:
    merged = MergedCatalog(Fake({"sleep_hours": AT}), Fake({"location": AT}))
    assert set(merged.last_seen()) == {"sleep_hours", "location"}


def test_같은_이름이_두_곳에_있으면_멈춘다() -> None:
    # 위치를 옮기다 만 상태 — 관측치에도 사건에도 있다. 어느 쪽 시각인지 모른다.
    merged = MergedCatalog(Fake({"location": AT}), Fake({"location": AT}))
    with pytest.raises(ValueError):
        merged.last_seen()
