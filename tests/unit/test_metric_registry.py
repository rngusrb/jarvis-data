from __future__ import annotations

from datetime import timedelta
from typing import Optional

import pytest

from src.core.metrics import Fold, Metric, MetricRegistry


def _card(kind: str, collector: Optional[str] = "단축어") -> Metric:
    return Metric(kind=kind, label=kind, fold=Fold.SUM, collector=collector)


def test_등록하고_찾는다() -> None:
    registry = MetricRegistry().register([_card("step_count")])
    found = registry.get("step_count")
    assert found is not None and found.fold is Fold.SUM


def test_모르는_지표는_None() -> None:
    assert MetricRegistry().get("혈중산소") is None


def test_같은_지표를_두_섹터가_주장하면_멈춘다() -> None:
    """조용히 덮어쓰면 어느 쪽이 이겼는지 알 수 없고, 저장소에서 값이 섞인다."""
    registry = MetricRegistry().register([_card("step_count")])
    with pytest.raises(ValueError, match="step_count"):
        registry.register([_card("step_count")])


def test_접은_지표는_active에서_빠진다() -> None:
    registry = MetricRegistry().register([_card("step_count"), _card("heart_rate_avg", None)])
    assert [m.kind for m in registry.active()] == ["step_count"]
    assert len(registry.all()) == 2


def test_끊김_기준을_카드가_갖는다() -> None:
    card = Metric(kind="x", label="x", fold=Fold.MEAN, collector="c", stale_after=timedelta(days=2))
    assert card.stale_after == timedelta(days=2)


def test_health_섹터_카드가_실제로_등록된다() -> None:
    from src.sectors.health import METRICS

    registry = MetricRegistry().register(METRICS)
    assert {m.kind for m in registry.active()} == {"sleep_hours", "resting_heart_rate"}

    # 접힌 지표도 카드는 남는다 — 과거 데이터가 있고, "안 만든 것"과 구별해야 한다.
    for retired in ("heart_rate_avg", "step_count"):
        card = registry.get(retired)
        assert card is not None and not card.active
