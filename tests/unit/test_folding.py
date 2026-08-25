"""접는 계산 — 도메인 없는 산수. 백필과 수신구가 같은 것을 지나간다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.folding import Sample, daily_mean, daily_sum, is_wrist, merge_spans

KST = timezone(timedelta(hours=9))
BASE = datetime(2026, 8, 18, 23, 0, tzinfo=KST)
# 같은 날 안에서 비교해야 하는 테스트용 — 23시 기준으로 +2시간 하면 날짜가 넘어간다
MORNING = datetime(2026, 8, 18, 9, 0, tzinfo=KST)


def test_겹치는_구간을_합친다() -> None:
    base = datetime(2026, 8, 18, 23, 0, tzinfo=KST)
    spans = [
        (base, base + timedelta(hours=4)),
        (base + timedelta(hours=2), base + timedelta(hours=8)),
    ]
    assert merge_spans(spans) == [(base, base + timedelta(hours=8))]


def test_안_겹치면_그대로_둔다() -> None:
    base = datetime(2026, 8, 18, 23, 0, tzinfo=KST)
    spans = [
        (base, base + timedelta(hours=1)),
        (base + timedelta(hours=3), base + timedelta(hours=4)),
    ]
    assert len(merge_spans(spans)) == 2


def test_워치를_손목으로_알아본다() -> None:
    assert is_wrist("구현규의 Apple Watch")
    assert is_wrist("현규 워치")
    assert not is_wrist("구현규의 iPhone")


def test_소스가_하나면_그대로_더한다() -> None:
    samples = [
        Sample(MORNING, MORNING + timedelta(hours=1), 100, "iPhone"),
        Sample(MORNING + timedelta(hours=2), MORNING + timedelta(hours=3), 200, "iPhone"),
    ]
    assert daily_sum(samples, "step_count", "s")[0].value == 300.0


def test_출처를_인자로_받는다() -> None:
    """플랫폼은 애플이라고 가정하지 않는다."""
    samples = [Sample(BASE, BASE, 70, "")]
    assert daily_mean(samples, "hr", "garmin")[0].source == "garmin"


def test_하루에_여러_번이면_마지막을_고른다() -> None:
    """퇴근처럼 여러 번 일어나도 의미 있는 건 하나다."""
    from src.core.folding import daily_last

    samples = [
        Sample(MORNING + timedelta(hours=4), MORNING + timedelta(hours=4), 13.0, ""),
        Sample(MORNING + timedelta(hours=8), MORNING + timedelta(hours=8), 17.5, ""),
    ]
    result = daily_last(samples, "work_departure", "shortcuts")[0]
    assert result.value == 17.5
    assert result.meta["samples"] == 2


def test_사건_시각이_관측치_시각을_덮지_않는다() -> None:
    """meta 키가 "at" 이면 응답 요약에서 관측치의 at 을 덮어쓴다."""
    from src.core.folding import daily_last

    result = daily_last([Sample(MORNING, MORNING, 9.0, "")], "work_arrival", "s")[0]
    assert result.at.hour == 0  # 하루 단위로 접혔다
    assert "at" not in result.meta
    assert "event_at" in result.meta


def test_처음_값을_고른다() -> None:
    from src.core.folding import daily_first

    samples = [
        Sample(MORNING + timedelta(hours=5), MORNING + timedelta(hours=5), 14.0, ""),
        Sample(MORNING, MORNING, 9.2, ""),
    ]
    assert daily_first(samples, "work_arrival", "s")[0].value == 9.2
