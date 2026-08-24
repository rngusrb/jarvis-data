from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.brain.context import ContextBlock, assemble, render
from src.core.models import Insight, Severity

NOW = datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc)
INSIGHT = Insight(trigger="t", summary="s", severity=Severity.NOTABLE, at=NOW)


class BrokenProvider:
    name = "broken"

    def fetch(self, insight: Insight, now: datetime) -> Optional[ContextBlock]:
        raise RuntimeError("캘린더 서버가 내려갔다")


class WorkingProvider:
    name = "working"

    def fetch(self, insight: Insight, now: datetime) -> Optional[ContextBlock]:
        return ContextBlock(label="오늘 일정", body="10시 회의")


class EmptyProvider:
    name = "empty"

    def fetch(self, insight: Insight, now: datetime) -> Optional[ContextBlock]:
        return None


def test_제공자가_터져도_나머지_맥락은_살아남는다() -> None:
    blocks = assemble([BrokenProvider(), WorkingProvider()], INSIGHT, NOW)
    assert len(blocks) == 1
    assert blocks[0].label == "오늘 일정"


def test_줄_맥락이_없으면_그냥_빠진다() -> None:
    assert assemble([EmptyProvider()], INSIGHT, NOW) == []


def test_렌더링은_라벨을_붙인다() -> None:
    rendered = render([ContextBlock(label="오늘 일정", body="10시 회의")])
    assert rendered == "[오늘 일정]\n10시 회의"
