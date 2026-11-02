"""맥 앱 사용 하루 요약 — 저장하지 않고 보여줄 때만 접는다."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import Trace
from src.sectors.interest.digest import app_days
from src.sectors.interest.traces import MAC_APP

KST = timezone(timedelta(hours=9))


def _span(app: str, day: int, start: str, minutes: float) -> Trace:
    """2026-11-<day> 한국 시각 start("HH:MM")부터 minutes 분. 저장소처럼 UTC 로 둔다."""
    h, m = map(int, start.split(":"))
    began = datetime(2026, 11, day, h, m, tzinfo=KST)
    seconds = int(minutes * 60)
    return Trace(
        source="mac_apps",
        kind="mac_app",
        text=f"{app} {round(minutes)}분",
        at=began.astimezone(timezone.utc),
        meta={
            "app": app,
            "seconds": seconds,
            "end": (began + timedelta(seconds=seconds)).isoformat(),
        },
    )


SINCE = datetime(2026, 11, 2, 5, 0, tzinfo=KST)  # 11-02(월) 하루가 시작할 때
UNTIL = datetime(2026, 11, 3, 12, 0, tzinfo=KST)  # 11-03(화) 낮


def test_하루는_새벽_5시에_바뀐다() -> None:
    lines = app_days(
        [
            _span("Code", 2, "23:00", 30),
            _span("Code", 3, "02:00", 20),  # 달력으로는 3일이지만 2일 밤의 연장이다
            _span("Chrome", 3, "06:00", 10),
        ],
        SINCE,
        UNTIL,
    )
    assert lines[0].startswith("11-02(월) 23:00~다음 날 02:20,")
    assert "모두 50분" in lines[0]
    assert lines[1].startswith("11-03(화) 06:00~06:10 (지금까지),")


def test_창_앞에서_잘린_날은_뺀다() -> None:
    # 11-02 는 10시부터만 창에 든다 — 넣으면 몇 시간만 센 날이 "덜 쓴 날"로 읽힌다.
    since = datetime(2026, 11, 2, 10, 0, tzinfo=KST)
    lines = app_days(
        [_span("Code", 2, "11:00", 60), _span("Code", 3, "08:00", 30)],
        since,
        UNTIL,
    )
    assert len(lines) == 1 and lines[0].startswith("11-03(화)")


def test_끝난_날에는_지금까지를_붙이지_않는다() -> None:
    lines = app_days([_span("Code", 2, "10:00", 30)], SINCE, UNTIL)
    assert "(지금까지)" not in lines[0]


def test_시간대로_나눈다() -> None:
    lines = app_days(
        [
            _span("Code", 2, "09:00", 30),
            _span("Code", 2, "13:00", 60),
            _span("Code", 2, "20:00", 90),
            _span("Code", 3, "01:00", 5),
        ],
        SINCE,
        UNTIL,
    )
    assert "[오전 30분 · 오후 1시간 · 저녁 1시간 30분 · 새벽 5분]" in lines[0]


def test_앱은_상위_다섯과_그_외로() -> None:
    spans = [_span(f"앱{i}", 2, f"{10 + i}:00", 50 - i * 5) for i in range(7)]
    line = app_days(spans, SINCE, UNTIL)[0]
    assert "앱0 50분, 앱1 45분, 앱2 40분, 앱3 35분, 앱4 30분, 그 외 2개 45분" in line
    assert "모두 4시간 5분" in line  # 50+45+40+35+30+25+20 = 245분


def test_짧은_사용도_빠뜨리지_않는다() -> None:
    line = app_days([_span("Finder", 2, "10:00", 20 / 60)], SINCE, UNTIL)[0]
    assert "Finder 1분 미만" in line


def test_분은_사사오입한다() -> None:
    # round() 는 짝수 쪽으로 반올림해 90초가 2분인데 270초는 4분이 됐다(리뷰).
    spans = [_span(app, 2, f"{h}:00", 1.5) for app, h in (("가", 10), ("나", 11), ("다", 13))]
    line = app_days(spans, SINCE, UNTIL)[0]
    assert "모두 5분" in line and "가 2분, 나 2분, 다 2분" in line


def test_자정_뒤에만_쓴_날은_시작에도_다음_날을_붙인다() -> None:
    # 월요일 하루의 사용이 화요일 01~03시뿐이면 "월요일 01시부터"로 읽혔다(리뷰 재현).
    line = app_days([_span("Code", 3, "01:00", 120)], SINCE, UNTIL)[0]
    assert line.startswith("11-02(월) 다음 날 01:00~03:00,")


def test_기록이_없는_날도_적는다() -> None:
    # 빼면 맥을 안 쓴 날인지 수집이 멈춘 날인지 모른 채 그날이 없던 것처럼 읽힌다.
    until = datetime(2026, 11, 4, 12, 0, tzinfo=KST)
    lines = app_days([_span("Code", 2, "10:00", 30), _span("Code", 4, "09:00", 30)], SINCE, until)
    assert [line.split(",")[0] for line in lines] == [
        "11-02(월) 10:00~10:30",
        "11-03(화) 기록 없음",
        "11-04(수) 09:00~09:30 (지금까지)",
    ]


def test_시간대_경계는_하루가_바뀌는_시각을_따른다() -> None:
    from src.sectors.interest.digest import DAY_STARTS, PARTS

    assert PARTS[0][1] == DAY_STARTS.hour and PARTS[-1][2] == DAY_STARTS.hour


def test_시각은_한국_시간으로_보여준다() -> None:
    # 저장소의 01:00 UTC 는 한국 10:00 이다.
    trace = _span("Code", 2, "10:00", 30)
    assert trace.at.hour == 1
    assert app_days([trace], SINCE, UNTIL)[0].startswith("11-02(월) 10:00~10:30")


def test_읽지_못한_줄은_빼고_남긴다(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    broken = Trace(
        source="mac_apps",
        kind="mac_app",
        text="Code 30분",
        at=datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc),
        meta={"app": "Code"},
    )
    lines = app_days([broken, _span("Chrome", 2, "11:00", 20)], SINCE, UNTIL)
    assert "Chrome 20분" in lines[0] and "Code" not in lines[0]
    assert "1줄은 앱 이름이나 쓴 시간이 없어" in caplog.text


def test_흔적이_없으면_빈_목록() -> None:
    assert app_days([], SINCE, UNTIL) == []


def test_카드가_요약을_들고_나간다() -> None:
    # 낱줄로는 회고에 안 넣고(reflect=False), 회고와 맥락은 이 요약을 읽는다.
    assert MAC_APP.reflect is False
    assert MAC_APP.digest is app_days


def test_기록이_있는_첫_날_앞은_적지_않는다() -> None:
    # 수집기가 생기기 전 날까지 "기록 없음"이면 "요즘 맥을 쓰기 시작했다"로 읽힌다(재리뷰).
    since = datetime(2026, 10, 27, 12, 0, tzinfo=KST)
    lines = app_days([_span("Code", 2, "10:00", 30)], since, UNTIL)
    assert [line.split(",")[0] for line in lines] == [
        "11-02(월) 10:00~10:30",
        "11-03(화) 기록 없음 (지금까지)",
    ]


def test_잘린_날에만_흔적이_있으면_빈_목록() -> None:
    # 보일 날이 전부 "기록 없음"이면 회고가 아무것도 없는 줄로 LLM 을 부른다(재리뷰 재현).
    since = datetime(2026, 11, 2, 9, 0, tzinfo=KST)
    assert app_days([_span("Code", 2, "10:00", 30)], since, UNTIL) == []
