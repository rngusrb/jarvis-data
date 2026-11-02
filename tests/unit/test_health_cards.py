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


NIGHT = datetime(2026, 10, 1, tzinfo=KST)
SEEN = {"sleep_hours": NIGHT, "resting_heart_rate": NIGHT}


def test_정오_넘어_깨는_날_기상_전송_전에는_경고하지_않는다() -> None:
    # 어젯밤(10/01) 기록의 "마지막 수신"은 10/01 00:00 이다. 오늘 13:00 이면 37시간 —
    # 36시간 기준이면 이미 "연결 문제"였다(2026-10-30 JAR-013 리뷰).
    assert _watch().check(SEEN, datetime(2026, 10, 2, 13, 0, tzinfo=KST)) is None
    assert _watch().check(SEEN, datetime(2026, 10, 2, 17, 59, tzinfo=KST)) is None


def test_진짜_끊겼으면_다음_날_저녁에_경고한다() -> None:
    # 48시간이면 자정에 울렸다(JAR-015 리뷰). 저녁 6시가 사람에게 말 걸 시각이다.
    assert _watch().check(SEEN, datetime(2026, 10, 2, 18, 1, tzinfo=KST)) is not None
