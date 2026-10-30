"""맥 앱 사용 구간 — 언제 끊고 언제 잇는가.

맥 없이 돌도록 표본(시각, 앞 앱, 입력 없이 흐른 시간)을 직접 넣는다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from collectors.mac_apps import Span, Tracker, to_line

# 10:00 KST
T0 = datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def feed(tracker: Tracker, samples: List[Tuple[float, Optional[str], float]]) -> List[Span]:
    """(분, 앞 앱, 입력 없이 흐른 초) 를 차례로 넣고 끝난 구간을 모은다."""
    done: List[Span] = []
    for minutes, app, idle in samples:
        done += tracker.observe(at(minutes), app, timedelta(seconds=idle))
    return done


def every_30s(start: float, end: float, app: Optional[str], idle: float = 3) -> List[Tuple]:
    count = int((end - start) * 2)
    return [(start + i / 2, app, idle) for i in range(count)]


def test_다른_앱이_앞으로_오면_바뀐_시각에_끝난다() -> None:
    done = feed(Tracker(), every_30s(0, 10, "Cursor") + [(10, "KakaoTalk", 1)])

    assert [(s.app, s.start, s.end) for s in done] == [("Cursor", at(0), at(10))]


def test_자리를_비운_시간은_앱에_붙이지_않는다() -> None:
    # 10:10 에 손을 뗐다. 5분이 지나야 알아채지만 끝은 마지막 입력 시각이다.
    away = [(10 + (i + 1) / 2, "Google Chrome", 30 * (i + 1)) for i in range(12)]
    done = feed(Tracker(), every_30s(0, 10, "Google Chrome") + away)

    assert [(s.start, s.end) for s in done] == [(at(0), at(10))]


def test_맥이_잠들었다_깨면_마지막_표본에서_끝난다() -> None:
    done = feed(Tracker(), every_30s(0, 5, "Cursor") + [(60, "Cursor", 1)])

    assert [(s.start, s.end) for s in done] == [(at(0), at(4.5))]


def test_정시를_넘으면_잘라서_시간대별로_나눈다() -> None:
    tracker = Tracker()
    done = feed(tracker, every_30s(50, 75, "Cursor") + [(75, "Finder", 1)])

    assert [(s.start, s.end) for s in done] == [(at(50), at(60)), (at(60), at(75))]
    assert all(s.start.hour == s.end.hour or s.end.minute == 0 for s in done)


def test_잠금_화면은_쓰는_앱이_아니다() -> None:
    locked = [(3, "loginwindow", 0)] + every_30s(3.5, 10, "loginwindow", idle=60)
    done = feed(Tracker(), every_30s(0, 3, "Cursor") + locked)

    assert [(s.app, s.end) for s in done] == [("Cursor", at(3))]


def test_한_표본만_보고_잠든_앱은_남기지_않는다() -> None:
    # 30초 간격이라 한 번 스친 앱도 보통은 다음 표본까지(30초) 잡힌다. 길이 0 인
    # 구간은 표본 하나만 찍히고 바로 잠들었을 때뿐이다.
    done = feed(Tracker(), [(0, "Finder", 1), (10, "Cursor", 1), (10.5, "Safari", 1)])

    assert [s.app for s in done] == ["Cursor"]


def test_다시_떠도_하던_구간을_잇는다() -> None:
    before = Tracker()
    feed(before, every_30s(0, 5, "Cursor"))
    revived = Tracker.restore(json.loads(json.dumps(before.dump())))

    done = feed(revived, every_30s(5, 8, "Cursor") + [(8, "Finder", 1)])

    assert [(s.start, s.end) for s in done] == [(at(0), at(8))]


def test_오래_쉬고_다시_뜨면_하던_구간을_마지막_표본에서_닫는다() -> None:
    before = Tracker()
    feed(before, every_30s(0, 5, "Cursor"))
    revived = Tracker.restore(before.dump())

    done = feed(revived, [(120, "Cursor", 1)])

    assert [(s.start, s.end) for s in done] == [(at(0), at(4.5))]


def test_보낼_줄은_배달부가_읽는_모양이다() -> None:
    line = json.loads(to_line([Span("Google Chrome", at(0), at(12))]))

    assert line["to"] == "traces"
    assert line["kind"] == "mac_app"
    assert line["traces"][0]["text"] == "Google Chrome 12분"
    assert line["traces"][0]["meta"]["seconds"] == 720
