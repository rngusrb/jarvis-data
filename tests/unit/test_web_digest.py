"""웹 방문 하루 요약 — 몇 곳, 어느 기기, 시간대, 많이 간 곳, 최근 검색어."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.models import Trace
from src.sectors.interest.digest import web_days
from src.sectors.interest.traces import WEB_VISIT

KST = timezone(timedelta(hours=9))
SINCE = datetime(2026, 11, 2, 5, 0, tzinfo=KST)  # 11-02(월) 하루가 시작할 때
UNTIL = datetime(2026, 11, 3, 12, 0, tzinfo=KST)  # 11-03(화) 낮


def _page(day: int, at: str, host: str, device: str = "맥") -> Trace:
    h, m = map(int, at.split(":"))
    when = datetime(2026, 11, day, h, m, tzinfo=KST)
    return Trace(
        source="mac_chrome",
        kind="web_visit",
        text=f"어떤 페이지 ({host})",
        at=when.astimezone(timezone.utc),
        meta={"url": f"https://{host}/", "host": host, "device": device},
    )


def _search(day: int, at: str, term: str, device: str = "맥") -> Trace:
    h, m = map(int, at.split(":"))
    when = datetime(2026, 11, day, h, m, tzinfo=KST)
    return Trace(
        source="mac_chrome",
        kind="web_visit",
        text=f"검색: {term}",
        at=when.astimezone(timezone.utc),
        meta={"url": "https://www.google.com/search", "host": "www.google.com", "device": device},
    )


def test_하루를_한_줄로() -> None:
    lines = web_days(
        [
            _page(2, "10:05", "github.com"),
            _page(2, "13:00", "github.com"),
            _search(2, "14:00", "전세 대출"),
            _page(2, "21:00", "youtube.com", device="다른 기기"),
            _page(3, "01:50", "youtube.com", device="다른 기기"),  # 2일 밤의 연장
        ],
        SINCE,
        UNTIL,
    )
    assert lines[0] == (
        "11-02(월) 10:05~다음 날 01:50, 5곳(맥 3 · 다른 기기 2) [오전 1 · 오후 2 · 저녁 1 · 새벽 1]"
        " — 많이 간 곳 github.com 2, youtube.com 2 · 검색 1번: '전세 대출'"
    )
    assert lines[1] == "11-03(화) 기록 없음 (지금까지)"


def test_검색은_되풀이를_하나로_최근_다섯만() -> None:
    terms = ["가", "가", "나", "다", "라", "마", "바"]
    traces = [_search(2, f"{10 + i}:00", term) for i, term in enumerate(terms)]
    line = web_days(traces, SINCE, UNTIL)[0]
    assert "검색 6번: '나', '다', '라', '마', '바'" in line


def test_검색만_한_날은_많이_간_곳이_없다() -> None:
    line = web_days([_search(2, "10:00", "날씨")], SINCE, UNTIL)[0]
    assert "많이 간 곳" not in line and "검색 1번: '날씨'" in line


def test_많이_간_곳은_다섯까지() -> None:
    hosts = ["a.com", "a.com", "a.com", "b.com", "b.com", "c.com", "d.com", "e.com", "f.com"]
    traces = [_page(2, f"{10 + i}:00", host) for i, host in enumerate(hosts)]
    line = web_days(traces, SINCE, UNTIL)[0]
    # 여섯째(f.com)는 빠지고, 검색이 없으면 거기서 끝난다.
    assert line.endswith("많이 간 곳 a.com 3, b.com 2, c.com 1, d.com 1, e.com 1")


def test_기기를_모르면_모른다고_센다() -> None:
    trace = Trace(
        source="mac_chrome",
        kind="web_visit",
        text="옛 페이지 (old.com)",
        at=datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc),
        meta={"host": "old.com"},
    )
    assert "1곳(기기 모름 1)" in web_days([trace], SINCE, UNTIL)[0]


def test_창_전체가_비면_빈_목록() -> None:
    assert web_days([], SINCE, UNTIL) == []


def test_카드가_요약을_들고_나간다() -> None:
    # 회고는 낱줄로 훑고(reflect), 반응·대화의 맥락은 이 요약을 읽는다. 민감하다는 사실은 그대로다.
    assert WEB_VISIT.digest is web_days
    assert WEB_VISIT.reflect is True and WEB_VISIT.sensitive is True
