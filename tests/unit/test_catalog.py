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


def test_같은_이름이_두_곳에_있으면_늦은_쪽을_쓰고_남긴다(caplog: pytest.LogCaptureFixture) -> None:
    # 위치를 옮기다 만 상태 — 관측치에도 사건에도 있다. 멈추면 그동안 수집 경고가 전부
    # 꺼진다(JAR-015 리뷰). 늦은 쪽이 실제로 마지막으로 들어온 것이다.
    earlier = AT.replace(day=1)
    merged = MergedCatalog(Fake({"location": earlier}), Fake({"location": AT}))

    assert merged.last_seen() == {"location": AT}
    assert "두 저장소" in caplog.text
