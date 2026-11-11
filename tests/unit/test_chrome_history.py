"""크롬 수집기 — 접는 칸, 이어 읽기, 늦게 들어온 방문, 한 번만 보내기.

진짜 크롬 DB 대신 같은 모양의 작은 DB 를 만든다. 크롬이 실제로 쓰는 열 중
수집기가 읽는 것만 있으면 된다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from collectors import chrome_history
from collectors.chrome_history import Plan, plan, snapshot, to_webkit

# 10:00 KST
T0 = datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class FakeChrome:
    def __init__(self, path: Path) -> None:
        self.path = path
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
            "CREATE TABLE visits (id INTEGER PRIMARY KEY AUTOINCREMENT, url INTEGER NOT NULL,"
            " visit_time INTEGER NOT NULL, originator_cache_guid TEXT);"
        )
        conn.commit()
        conn.close()

    def visit(self, when: datetime, page: str, other_device: bool = False) -> int:
        conn = sqlite3.connect(self.path)
        url = f"https://example.com/{page}"
        row = conn.execute("SELECT id FROM urls WHERE url = ?", (url,)).fetchone()
        url_id = (
            row[0]
            if row
            else conn.execute("INSERT INTO urls (url, title) VALUES (?, ?)", (url, page)).lastrowid
        )
        visit_id = conn.execute(
            "INSERT INTO visits (url, visit_time, originator_cache_guid) VALUES (?, ?, ?)",
            (url_id, to_webkit(when), "phone-guid" if other_device else ""),
        ).lastrowid
        conn.commit()
        conn.close()
        assert visit_id is not None
        return int(visit_id)

    def retitle(self, page: str, title: str) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute(
            "UPDATE urls SET title = ? WHERE url = ?", (title, f"https://example.com/{page}")
        )
        conn.commit()
        conn.close()

    def delete(self, visit_id: int) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute("DELETE FROM visits WHERE id = ?", (visit_id,))
        conn.commit()
        conn.close()


class Collector:
    """실행마다 커서를 이어받는 수집기. 보낸 흔적을 전부 모은다."""

    def __init__(self, chrome: FakeChrome) -> None:
        self.chrome = chrome
        self.after_id: Optional[int] = None
        self.sent_above: List[int] = []
        self.sent: List[Dict[str, Any]] = []

    def run(self, now: datetime, since: Optional[datetime] = None) -> Plan:
        with snapshot(self.chrome.path) as conn:
            found = plan(
                conn, self.after_id, self.sent_above, since or now - timedelta(days=1), now
            )
        self.sent += found.traces
        self.after_id, self.sent_above = found.after_id, found.sent_above
        return found


def visits_sent(traces: List[Dict[str, Any]]) -> int:
    return sum(int(t["meta"].get("visits", 1)) for t in traces)


@pytest.fixture
def chrome(tmp_path: Path) -> FakeChrome:
    return FakeChrome(tmp_path / "History")


def test_같은_칸의_같은_페이지는_하나로_접고_방문수는_그_줄에_붙는다(chrome: FakeChrome) -> None:
    # 예전엔 A·B·A 순서면 두 번째 A 의 방문 수가 바로 앞 줄인 B 에 붙었다.
    chrome.visit(at(0), "a")
    chrome.visit(at(5), "b")
    chrome.visit(at(10), "a")

    traces = Collector(chrome).run(now=at(60)).traces

    by_text = {t["text"]: t for t in traces}
    assert by_text["a (example.com)"]["meta"]["visits"] == 2
    assert "visits" not in by_text["b (example.com)"]["meta"]
    assert by_text["a (example.com)"]["at"] == at(0).isoformat()


def test_안_닫힌_칸은_미루고_칸이_닫히면_한꺼번에_접는다(chrome: FakeChrome) -> None:
    collector = Collector(chrome)
    chrome.visit(at(10), "a")
    chrome.visit(at(35), "b")  # 10:30~11:00 칸. 지금 10:40 이라 안 닫혔다

    found = collector.run(now=at(40))
    assert [t["text"] for t in found.traces] == ["a (example.com)"]
    assert found.waiting == 1

    chrome.visit(at(50), "b")
    found = collector.run(now=at(70))

    assert [(t["text"], t["meta"]["visits"]) for t in found.traces] == [("b (example.com)", 2)]


def test_한_방문은_한_번만_보낸다(chrome: FakeChrome) -> None:
    # 미룬 칸, 늦게 들어온 다른 기기 방문, 칸 경계가 섞여도 방문 수 합이 정확해야 한다.
    collector = Collector(chrome)
    total = 0
    for minute in (2, 12, 31, 33, 58, 61):
        chrome.visit(at(minute), "a")
        total += 1
    collector.run(now=at(40))
    chrome.visit(at(20), "a", other_device=True)  # 이미 보낸 칸에 늦게 들어왔다
    total += 1
    collector.run(now=at(65))
    chrome.visit(at(75), "b")
    total += 1
    collector.run(now=at(120))
    collector.run(now=at(180))

    assert visits_sent(collector.sent) == total
    keys = [(t["at"], t["text"]) for t in collector.sent]
    assert len(keys) == len(set(keys))


def test_늦게_들어온_더_이른_방문은_이미_보낸_방문을_다시_세지_않는다(chrome: FakeChrome) -> None:
    # 리뷰 재현: 맥 10:12 방문을 보낸 뒤, 폰의 10:05 방문이 늦게 동기화됐다.
    # 칸을 통째로 다시 접으면 (10:05, 둘 다, 2회) 줄이 새로 생겨 10:12 를 두 번 센다.
    collector = Collector(chrome)
    chrome.visit(at(12), "a")
    collector.run(now=at(60))
    chrome.visit(at(5), "a", other_device=True)

    late = collector.run(now=at(70)).traces

    assert len(late) == 1
    assert late[0]["at"] == at(5).isoformat()
    assert late[0]["meta"]["device"] == "다른 기기"
    assert "visits" not in late[0]["meta"]


def test_제목이_바뀌어도_이미_보낸_방문을_다시_보내지_않는다(chrome: FakeChrome) -> None:
    # 리뷰 재현: "Inbox (3)" 로 보낸 방문이, 제목이 "Inbox (5)" 로 바뀐 뒤 칸이 다시
    # 접히면서 새 줄로 또 들어갔다.
    collector = Collector(chrome)
    chrome.visit(at(10), "mail")
    collector.run(now=at(40))
    chrome.retitle("mail", "Inbox (5)")
    chrome.visit(at(20), "other", other_device=True)

    traces = collector.run(now=at(70)).traces

    assert [t["text"] for t in traces] == ["other (example.com)"]


def test_크롬_기록이_새로_만들어지면_처음부터_다시_읽는다(
    chrome: FakeChrome, tmp_path: Path
) -> None:
    # 리뷰 재현: 모든 기간 삭제나 손상 복구 뒤엔 번호가 1부터 다시 붙는다. 커서를
    # 그대로 두면 새 방문이 전부 커서 아래로 숨어 매시간 "흔적 0개"로 멈춘다.
    collector = Collector(chrome)
    for minute in range(0, 25, 5):
        chrome.visit(at(minute), "old")
    collector.run(now=at(60))
    assert collector.after_id == 5

    chrome.path.unlink()
    rebuilt = FakeChrome(chrome.path)
    rebuilt.visit(at(70), "new")
    found = collector.run(now=at(120))

    assert found.restarted
    assert [t["text"] for t in found.traces] == ["new (example.com)"]
    assert collector.after_id == 1


def test_최근_기록만_지워도_다시_읽지_않는다(chrome: FakeChrome) -> None:
    # 마지막 방문을 지우면 MAX(id) 는 커서보다 작아지지만 번호 최고 기록은 그대로다.
    collector = Collector(chrome)
    chrome.visit(at(10), "a")
    last = chrome.visit(at(20), "b")
    collector.run(now=at(60))
    chrome.delete(last)

    found = collector.run(now=at(70))

    assert not found.restarted
    assert found.traces == []


def test_늦게_동기화된_방문도_다음에_들어간다(chrome: FakeChrome) -> None:
    # 아이폰에서 본 페이지는 몇 시간 늦게 맥 DB 에 들어온다. 시각은 원래 시각 그대로.
    collector = Collector(chrome)
    chrome.visit(at(10), "a")
    collector.run(now=at(60))

    chrome.visit(at(-180), "phone-page", other_device=True)
    traces = collector.run(now=at(70)).traces

    assert [t["text"] for t in traces] == ["phone-page (example.com)"]
    assert traces[0]["meta"]["device"] == "다른 기기"


def test_새_방문이_없으면_커서를_그대로_둔다(chrome: FakeChrome) -> None:
    collector = Collector(chrome)
    last = chrome.visit(at(10), "a")
    collector.run(now=at(60))

    found = collector.run(now=at(70))

    assert (found.traces, found.after_id, found.waiting) == ([], last, 0)


def _run(chrome: FakeChrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    monkeypatch.setenv("JARVIS_STATE_DIR", str(tmp_path / "state"))
    code = chrome_history.main(["--profile", str(chrome.path), "--hours", "48"])
    state_file = tmp_path / "state" / "chrome_history.json"
    outbox = sorted((tmp_path / "state" / "outbox").glob("mac_chrome-*.jsonl"))
    return {
        "code": code,
        "state": json.loads(state_file.read_text()) if state_file.exists() else {},
        "lines": [json.loads(line) for f in outbox for line in f.read_text().splitlines()],
    }


def test_보낼_상자에_적고_커서를_옮긴다(
    chrome: FakeChrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 서버로 직접 보내지 않는다. 토큰도 필요 없다 — 보내는 건 배달부 몫이다.
    last = chrome.visit(datetime.now(timezone.utc) - timedelta(hours=2), "a")

    result = _run(chrome, tmp_path, monkeypatch)

    assert result["code"] == 0
    assert [(line["to"], line["kind"]) for line in result["lines"]] == [("traces", "web_visit")]
    assert result["state"][str(chrome.path)]["after_id"] == last


def test_다시_돌려도_같은_방문을_다시_적지_않는다(
    chrome: FakeChrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome.visit(datetime.now(timezone.utc) - timedelta(hours=2), "a")
    _run(chrome, tmp_path, monkeypatch)

    result = _run(chrome, tmp_path, monkeypatch)

    assert len(result["lines"]) == 1


def test_상자에_적은_줄을_배달부가_그대로_보낸다(
    chrome: FakeChrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collectors.forward import files, forward

    monkeypatch.setenv("JARVIS_INBOX", str(tmp_path / "no-icloud" / "jarvis"))
    chrome.visit(datetime.now(timezone.utc) - timedelta(hours=2), "a")
    _run(chrome, tmp_path, monkeypatch)
    got: List[Any] = []

    report, _, _ = forward(
        files(create=False).paths, {}, lambda route, body: got.append((route, body)) or {}
    )

    assert report.sent == 1
    assert got[0][0] == "/ingest/traces"
    assert [t["text"] for t in got[0][1]["traces"]] == ["a (example.com)"]


def test_상자에_못_쓰면_커서를_옮기지_않는다(
    chrome: FakeChrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 순서가 반대(커서 먼저)면 그 사이에 죽었을 때 방문이 상자에도 커서에도 없이 사라진다.
    chrome.visit(datetime.now(timezone.utc) - timedelta(hours=2), "a")

    def disk_full(path: Path, lines: Any) -> None:
        raise OSError("디스크가 찼다")

    monkeypatch.setattr(chrome_history, "append_lines", disk_full)
    with pytest.raises(OSError):
        _run(chrome, tmp_path, monkeypatch)

    assert not (tmp_path / "state" / "chrome_history.json").exists()


def test_검색은_검색어로_적는다() -> None:
    from collectors.chrome_history import describe

    assert describe("https://www.google.com/search?q=%EC%A0%84%EC%84%B8", "전세 - Google 검색") == (
        "검색: 전세"
    )
    assert describe("https://www.youtube.com/results?search_query=kbo", "YouTube") == "검색: kbo"


def test_검색_사이트의_보통_페이지는_버리지_않는다() -> None:
    # 2026-11-02 까지 검색어가 없으면 다 버려 GitHub 1,045건, 유튜브 영상 179건이 빠졌다
    # (JAR-020 리뷰).
    from collectors.chrome_history import describe

    assert (
        describe("https://github.com/rngusrb/jarvis-data/pull/43", "PR 43") == "PR 43 (github.com)"
    )
    assert describe("https://www.youtube.com/watch?v=abc", "경기 하이라이트") == (
        "경기 하이라이트 (www.youtube.com)"
    )
    assert (
        describe("https://www.google.com/maps/place/x", "Google 지도")
        == "Google 지도 (www.google.com)"
    )


def test_첫_화면과_이동용_주소는_흔적이_아니다() -> None:
    from collectors.chrome_history import describe

    for url in (
        "https://www.google.com/",
        "https://www.google.com/webhp?hl=ko",
        "https://www.google.com/search?tbm=isch",
        "https://www.google.com/url?sa=t&url=https://example.com",
        "https://www.google.com/aclk?ai=x&adurl=https://shop.example",
        "https://search.naver.com/p/crd/rd?m=1&u=https://example.com",
        "https://www.youtube.com/",
        "https://www.youtube.com/results",
        "https://github.com/",
        "https://github.com/notifications?query=reason%3Aassign",
        "https://github.com/login?return_to=x",
    ):
        assert describe(url, "목적지 제목") is None, url


def test_이동용_주소는_검색어가_있어도_검색이_아니다() -> None:
    # 구글 /url?q=<목적지> 가 "검색: https://…"로 적혔다(JAR-020 재리뷰).
    from collectors.chrome_history import describe

    assert describe("https://www.google.com/url?q=https://example.com&sa=U", "Example") is None


def test_검색_사이트마다_보통_페이지가_아닌_경로를_적어_둔다() -> None:
    # 한쪽에만 호스트를 더하면 describe 가 KeyError 로 수집기 전체를 멈춘다.
    from collectors.chrome_history import NOT_PAGES, REDIRECTS, SEARCH_HOSTS

    assert set(NOT_PAGES) == set(SEARCH_HOSTS)
    assert set(REDIRECTS) <= set(SEARCH_HOSTS)


def test_권한이_막히면_못_찾았다가_아니라_권한이라고_적는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS 27.2(2026-11-10)부터 launchd 의 파이썬이 크롬 폴더를 못 읽는다. glob 은 빈 목록을
    줘서 이틀 동안 "프로필을 찾지 못했다"고만 적었다."""

    def blocked(self: Path) -> Any:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "iterdir", blocked)
    with pytest.raises(SystemExit, match="권한이 없다"):
        chrome_history.find_profile(tmp_path)
    monkeypatch.undo()
    with pytest.raises(SystemExit, match="찾지 못했다"):
        chrome_history.find_profile(tmp_path)  # 빈 폴더는 그대로
