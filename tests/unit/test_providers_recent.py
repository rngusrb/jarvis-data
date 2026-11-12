"""최근 값 제공자.

이게 없으면 자비스가 "수집은 되고 있다"만 알고 "값이 뭔지"는 모른다.
실제로 그 상태에서 "데이터 값이 없어서 판단할 수 없어"라고 답했다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.brain.providers import RecentValuesProvider
from src.core.metrics import Fold, Metric
from src.core.models import Observation
from src.storage.sqlite import SQLiteStore

NOW = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)
SLEEP = Metric(kind="sleep_hours", label="수면", fold=Fold.SPANS, collector="단축어")
STEPS = Metric(kind="step_count", label="걸음수", fold=Fold.SUM, collector=None)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "t.db")

    def obs(kind: str, value: float, days: int) -> Observation:
        return Observation(
            source="apple_health", kind=kind, value=value, at=NOW - timedelta(days=days)
        )

    store.write(
        [obs("sleep_hours", v, d) for d, v in ((2, 5.16), (1, 3.06), (0, 4.20))]
        + [obs("step_count", 646.0, 1)]
    )
    return store


def test_실제_값이_프롬프트에_들어간다(tmp_path: Path) -> None:
    block = RecentValuesProvider(source=_store(tmp_path), metrics=[SLEEP]).fetch(None, NOW)
    assert block is not None
    assert "5.16" in block.body and "3.06" in block.body and "4.2" in block.body


def test_접힌_지표는_빼고_보여준다(tmp_path: Path) -> None:
    """과거 데이터만 남은 것을 현재 상태인 양 보여주면 두 달 전 숫자를 읽는다."""
    block = RecentValuesProvider(source=_store(tmp_path), metrics=[SLEEP, STEPS]).fetch(None, NOW)
    assert block is not None
    assert "646" not in block.body
    assert "걸음수" not in block.body


def test_값이_하나도_없으면_침묵한다(tmp_path: Path) -> None:
    empty = SQLiteStore(tmp_path / "empty.db")
    assert RecentValuesProvider(source=empty, metrics=[SLEEP]).fetch(None, NOW) is None


def test_풀어_쓰는_지표는_가장_최근_것을_그_말로(tmp_path: Path) -> None:
    """예보처럼 값 하나로 안 읽히는 관측치는 섹터가 풀어 쓴 한 줄로 — 숫자 줄로 보이면
    "강수확률 70"만 남는다(JAR-043)."""
    store = SQLiteStore(tmp_path / "t.db")
    now = datetime(2026, 11, 11, 21, 0, tzinfo=timezone.utc)
    store.write(
        [
            Observation(source="kma", kind="forecast", value=v, at=now - timedelta(hours=h))
            for v, h in ((20.0, 6), (70.0, 3))
        ]
    )
    described = Metric(
        kind="forecast",
        label="예보",
        fold=Fold.RAW,
        collector="서버",
        describe=lambda o, at: f"{o.value:g}% 발표 하나",
    )
    block = RecentValuesProvider(source=store, metrics=[described]).fetch(None, now)
    assert block is not None and block.body == "- 예보(forecast): 70% 발표 하나"
