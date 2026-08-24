from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from src.brain.providers import CollectionStatusProvider
from src.core.metrics import Fold, Metric
from src.core.models import Insight, Severity

NOW = datetime(2026, 8, 24, 11, 0, tzinfo=timezone.utc)
INSIGHT = Insight(trigger="t", summary="s", severity=Severity.NOTABLE, at=NOW)


class FakeCatalog:
    def __init__(self, last_seen: Dict[str, datetime]) -> None:
        self._last_seen = last_seen

    def last_seen(self) -> Dict[str, datetime]:
        return self._last_seen


def _cards(collectors: Dict[str, Optional[str]]) -> List[Metric]:
    return [Metric(kind=k, label=k, fold=Fold.SUM, collector=v) for k, v in collectors.items()]


def _fetch(last_seen: Dict[str, datetime], collectors: Dict[str, Optional[str]]) -> str:
    block = CollectionStatusProvider(
        catalog=FakeCatalog(last_seen), metrics=_cards(collectors)
    ).fetch(INSIGHT, NOW)
    assert block is not None
    return block.body


def test_수집기가_없으면_없다고_말한다() -> None:
    """이걸 몰라서 "권한 설정을 확인하라"는 헛조언이 나왔다."""
    body = _fetch({}, {})
    assert body == ""

    body = _fetch({"step_count": NOW - timedelta(days=5)}, {})
    assert "step_count" in body
    assert "수집기 없음" in body


def test_최근에_들어왔으면_정상이라고_한다() -> None:
    body = _fetch(
        {"sleep_hours": NOW - timedelta(hours=11)},
        {"sleep_hours": "단축어가 기상할 때 전송"},
    )
    assert "정상" in body
    assert "끊김" not in body


def test_오래됐으면_끊겼다고_말한다() -> None:
    body = _fetch(
        {"step_count": NOW - timedelta(hours=131)},
        {"step_count": "단축어가 기상할 때 전송"},
    )
    assert "131시간째 끊김" in body


def test_선언은_있는데_한_번도_안_들어온_경우() -> None:
    body = _fetch({}, {"heart_rate_avg": "단축어가 기상할 때 전송"})
    assert "아직 한 번도" in body


def test_수집기_선언과_실제_데이터를_모두_훑는다() -> None:
    body = _fetch(
        {"sleep_hours": NOW - timedelta(hours=2), "step_count": NOW - timedelta(days=6)},
        {"sleep_hours": "단축어", "heart_rate_avg": "단축어"},
    )
    for kind in ("sleep_hours", "step_count", "heart_rate_avg"):
        assert kind in body


def test_일부러_접은_지표는_되살리라고_하지_않는다() -> None:
    """안 만든 것과 버린 것은 다르다. 섞으면 자비스가 버린 지표를 조른다."""
    body = _fetch(
        {"heart_rate_avg": NOW - timedelta(days=6)},
        {"heart_rate_avg": None},
    )
    assert "수집 중단" in body
    assert "아직 만들지" not in body


def test_접었다는데_데이터가_들어오면_그대로_말한다() -> None:
    """선언과 현실이 어긋났으면 선언 쪽이 낡은 것이다."""
    body = _fetch({"heart_rate_avg": NOW - timedelta(hours=2)}, {"heart_rate_avg": None})
    assert "선언이 낡았다" in body
