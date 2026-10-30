"""접는 계산 — 도메인 없는 산수. 백필과 수신구가 같은 것을 지나간다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.folding import (
    Sample,
    daily_mean,
    daily_sum,
    is_wrist,
    merge_spans,
    nights_from_spans,
)

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


def _night(*pieces: tuple[float, float]) -> list[tuple[datetime, datetime]]:
    """(시작, 끝)을 23시 기준 시간 수로 받는다."""
    return [(BASE + timedelta(hours=a), BASE + timedelta(hours=b)) for a, b in pieces]


def test_잠든_시각과_깬_시각을_남긴다() -> None:
    # 워치는 밤을 조각으로 적고, 중간에 깬 몇 분은 수면이 아니라 빠져 있다.
    spans = _night((0.5, 2.0), (2.1, 4.0), (4.2, 7.5))

    night = nights_from_spans(spans, kind="sleep_hours", source="apple_health")[0]

    assert night.meta["onset"] == (BASE + timedelta(hours=0.5)).isoformat()
    assert night.meta["wake"] == (BASE + timedelta(hours=7.5)).isoformat()
    assert night.meta["segments"] == 3


def test_낮잠은_잠든_시각이_되지_않는다() -> None:
    # 정오 경계 때문에 오후 낮잠(다음날 15~16시)이 그다음 밤과 같이 묶인다.
    nap = (BASE + timedelta(hours=16), BASE + timedelta(hours=17))
    sleep = (BASE + timedelta(hours=24.5), BASE + timedelta(hours=31))

    night = nights_from_spans([nap, sleep], kind="sleep_hours", source="apple_health")[0]

    assert night.meta["onset"] == sleep[0].isoformat()
    assert night.meta["wake"] == sleep[1].isoformat()
    assert night.value == 7.5  # 합계는 그대로 낮잠까지 더한다


def test_두_기기가_겹쳐도_시각과_합계가_부풀지_않는다() -> None:
    watch = _night((0.0, 7.0))
    phone = _night((0.5, 7.5))

    night = nights_from_spans(watch + phone, kind="sleep_hours", source="apple_health")[0]

    assert night.value == 7.5
    assert night.meta["onset"] == (BASE).isoformat()
    assert night.meta["wake"] == (BASE + timedelta(hours=7.5)).isoformat()


def test_정오를_넘긴_잠은_한_밤이다() -> None:
    # 리뷰 재현: 04:30~13:30 을 조각마다 배정하면 정오 뒤 꼬리가 다음 밤이 되어, 깬 시각이
    # 정오 직후로 잘리고 다음 날 "어젯밤 1.5시간" 짜리 행이 생겼다.
    spans = _night((5.5, 9.0), (9.1, 12.9), (13.0, 13.4), (13.5, 14.5))

    nights = nights_from_spans(spans, kind="sleep_hours", source="apple_health")

    assert len(nights) == 1
    assert nights[0].meta["wake"] == (BASE + timedelta(hours=14.5)).isoformat()
    assert nights[0].value == 8.7


def test_한_시간_넘게_깨어_있었으면_다른_잠이다() -> None:
    joined = nights_from_spans(_night((0, 3), (3.8, 7)), kind="s", source="a")[0]
    split = nights_from_spans(_night((0, 3), (4.2, 5)), kind="s", source="a")[0]

    assert joined.meta["wake"] == (BASE + timedelta(hours=7)).isoformat()
    # 틈이 1시간을 넘으면 덩어리가 갈리고, 긴 쪽(0~3시)이 잠든·깬 시각이 된다.
    assert split.meta["wake"] == (BASE + timedelta(hours=3)).isoformat()


def test_마지막이_아니라_가장_긴_덩어리다() -> None:
    # 밤잠 뒤 아침에 짧게 다시 잔 것. 마지막 덩어리를 쓰면 잠든 시각이 아침이 된다.
    night = nights_from_spans(_night((0, 6), (8, 9)), kind="s", source="a")[0]

    assert night.meta["onset"] == BASE.isoformat()
    assert night.meta["wake"] == (BASE + timedelta(hours=6)).isoformat()
