from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from src.brain.context import ContextBlock, assemble, render
from src.core.models import Insight, Severity

NOW = datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc)
INSIGHT = Insight(trigger="t", summary="s", severity=Severity.NOTABLE, at=NOW)


class BrokenProvider:
    name = "broken"

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        raise RuntimeError("캘린더 서버가 내려갔다")


class WorkingProvider:
    name = "working"

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        return ContextBlock(label="오늘 일정", body="10시 회의")


class EmptyProvider:
    name = "empty"

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
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


def test_신호_없이도_맥락이_조립된다() -> None:
    """사고 재현 — 2026-10-02.

    `fetch(insight: Insight, ...)` 로 신호를 필수로 두었더니 대화에서 쓸 수가
    없어 가짜 신호를 만들었다. 그 가짜는 observations 가 비어서 추이 제공자가
    침묵했고, 자비스는 데이터가 멀쩡히 있는데도 "값이 없어서 판단할 수 없어"
    라고 답했다.
    """
    from src.brain.providers import CollectionStatusProvider
    from src.core.metrics import Fold, Metric

    @dataclass
    class FakeCatalog:
        def last_seen(self) -> Dict[str, datetime]:
            return {"sleep_hours": NOW - timedelta(hours=3)}

    provider = CollectionStatusProvider(
        catalog=FakeCatalog(),
        metrics=[Metric(kind="sleep_hours", label="수면", fold=Fold.SPANS, collector="단축어")],
    )
    blocks = assemble([provider], None, NOW)
    assert blocks and "수면" in blocks[0].body


def test_신호에_기대는_제공자는_신호가_없으면_침묵한다() -> None:
    from src.brain.providers import ObservationTrendProvider

    assert ObservationTrendProvider().fetch(None, NOW) is None
