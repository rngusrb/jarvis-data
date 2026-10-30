"""크롬 수집기 — 접는 칸, 이어 읽기, 늦게 들어온 방문.

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
from collectors.chrome_history import plan, snapshot, to_webkit
from collectors.common import Retry

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


def _plan(
    chrome: FakeChrome, after_id: Optional[int], now: datetime, since: Optional[datetime] = None
) -> Any:
    with snapshot(chrome.path) as conn:
        return plan(conn, after_id, since or now - timedelta(days=1), now)


def _texts(traces: List[Dict[str, Any]]) -> List[str]:
    return [t["text"] for t in traces]


@pytest.fixture
def chrome(tmp_path: Path) -> FakeChrome:
    return FakeChrome(tmp_path / "History")


def test_같은_칸의_같은_페이지는_하나로_접고_방문수는_그_줄에_붙는다(chrome: FakeChrome) -> None:
    # 예전엔 A·B·A 순서면 두 번째 A 의 방문 수가 바로 앞 줄인 B 에 붙었다.
    chrome.visit(at(0), "a")
    chrome.visit(at(5), "b")
    chrome.visit(at(10), "a")

    traces, _, _ = _plan(chrome, None, now=at(60))

    by_text = {t["text"]: t for t in traces}
    assert by_text["a (example.com)"]["meta"]["visits"] == 2
    assert "visits" not in by_text["b (example.com)"]["meta"]
    assert by_text["a (example.com)"]["at"] == at(0).isoformat()


def test_어디서부터_읽든_같은_칸은_같은_흔적이_된다(chrome: FakeChrome) -> None:
    # 예전 사고: 10:02 와 10:12 에 같은 페이지. 10:05 부터 읽은 실행은 10:12 를
    # 새 줄로 만들어 보냈다 — 이미 10:02 줄에 접혀 들어간 방문인데.
    chrome.visit(at(2), "a")
    chrome.visit(at(12), "a")

    whole, _, _ = _plan(chrome, None, now=at(60), since=at(0))
    late_start, _, _ = _plan(chrome, None, now=at(60), since=at(5))

    assert late_start == whole
    assert [t["at"] for t in whole] == [at(2).isoformat()]


def test_안_닫힌_칸은_미루고_커서는_그_앞에서_멈춘다(chrome: FakeChrome) -> None:
    first = chrome.visit(at(10), "a")
    waiting = chrome.visit(at(35), "b")  # 10:30~11:00 칸, 지금 10:40 이라 안 닫혔다

    traces, cursor, held = _plan(chrome, None, now=at(40))

    assert _texts(traces) == ["a (example.com)"]
    assert held == 1
    assert cursor == waiting - 1 == first

    # 칸이 닫힌 뒤엔 미뤘던 방문이 간다.
    traces, cursor, held = _plan(chrome, cursor, now=at(70))
    assert _texts(traces) == ["b (example.com)"]
    assert (cursor, held) == (waiting, 0)


def test_늦게_동기화된_방문도_다음에_들어간다(chrome: FakeChrome) -> None:
    # 아이폰에서 본 페이지는 몇 시간 늦게 맥 DB 에 들어온다. 시각은 원래 시각 그대로.
    chrome.visit(at(10), "a")
    _, cursor, _ = _plan(chrome, None, now=at(60))

    chrome.visit(at(-180), "phone-page", other_device=True)
    traces, _, _ = _plan(chrome, cursor, now=at(70))

    assert _texts(traces) == ["phone-page (example.com)"]
    assert traces[0]["meta"]["device"] == "다른 기기"


def test_새_방문이_없으면_커서를_그대로_둔다(chrome: FakeChrome) -> None:
    last = chrome.visit(at(10), "a")
    assert _plan(chrome, last, now=at(60)) == ([], last, 0)


def _run(
    chrome: FakeChrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, post: Any
) -> Dict[str, Any]:
    monkeypatch.setenv("JARVIS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JARVIS_INGEST_TOKEN", "t")
    monkeypatch.setattr(chrome_history, "post", post)
    code = chrome_history.main(["--profile", str(chrome.path), "--hours", "48"])
    state_file = tmp_path / "state" / "chrome_history.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    return {"code": code, "state": state}


def test_보내고_나면_커서를_옮긴다(
    chrome: FakeChrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    last = chrome.visit(datetime.now(timezone.utc) - timedelta(hours=2), "a")
    sent: List[Dict[str, Any]] = []

    def post(server: str, auth: str, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        sent.append(body)
        return {"written": len(body["traces"])}

    result = _run(chrome, tmp_path, monkeypatch, post)

    assert result["code"] == 0
    assert sent[0]["kind"] == "web_visit"
    assert result["state"][str(chrome.path)]["after_id"] == last


def test_보내기에_실패하면_커서를_옮기지_않는다(
    chrome: FakeChrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome.visit(datetime.now(timezone.utc) - timedelta(hours=2), "a")

    def post(server: str, auth: str, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        raise Retry("서버에 닿지 않는다")

    result = _run(chrome, tmp_path, monkeypatch, post)

    assert result["code"] == 1
    assert result["state"] == {}
