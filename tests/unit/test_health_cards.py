"""health 카드 — 밤 단위 지표의 끊김 기준."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.sectors.health.metrics import METRICS
from src.triggers.collection import CollectionWatch, Watched

KST = timezone(timedelta(hours=9))


def _watch() -> CollectionWatch:
    return CollectionWatch(
        sources=[
            Watched(m.kind, m.label, m.collector or "", m.via or "", m.stale_after)
            for m in METRICS
            if m.active
        ]
    )


def test_정오_넘어_깨는_날_기상_전송_전에는_경고하지_않는다() -> None:
    # 어젯밤(10/01) 기록의 "마지막 수신"은 10/01 00:00 이다. 오늘 13:00 이면 37시간 —
    # 36시간 기준이면 이미 "연결 문제"였다(2026-10-30 JAR-013 리뷰).
    night = datetime(2026, 10, 1, tzinfo=KST)
    seen = {"sleep_hours": night, "resting_heart_rate": night}

    assert _watch().check(seen, datetime(2026, 10, 2, 13, 0, tzinfo=KST)) is None


def test_다음_날이_다_가도록_안_오면_경고한다() -> None:
    night = datetime(2026, 10, 1, tzinfo=KST)
    seen = {"sleep_hours": night, "resting_heart_rate": night}

    assert _watch().check(seen, datetime(2026, 10, 3, 1, 0, tzinfo=KST)) is not None
