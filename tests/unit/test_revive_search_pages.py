"""옛 규칙이 버린 검색 사이트 페이지 되살리기 (JAR-020).

옛 수집기가 처리한 번호 가운데 옛 규칙이 버리고 새 규칙은 남기는 방문만 — 이미 보낸 방문도,
새 수집기가 처리할 커서 위도 건드리지 않는다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

import pytest

from collectors import revive_search_pages as revive
from collectors.chrome_history import STATE_NAME, snapshot, to_webkit
from collectors.common import save_state

T0 = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)

# 번호(1부터) 순서대로. 옆은 옛 규칙과 새 규칙의 판정.
PAGES: List[Tuple[str, str]] = [
    ("https://github.com/me/repo", "me/repo"),  # 1 옛: 버림 · 새: 남김 → 되살림
    ("https://www.google.com/search?q=x", "x - Google 검색"),  # 2 검색 — 옛날에도 보냈다
    ("https://www.google.com/", "Google"),  # 3 첫 화면 — 둘 다 버림
    ("https://example.com/a", "A"),  # 4 보통 사이트 — 옛날에도 보냈다
    ("https://www.youtube.com/watch?v=1", "영상"),  # 5 옛: 버림 · 새: 남김 → 되살림
    ("https://www.google.com/url?q=https://e.com", "E"),  # 6 이동용 — 둘 다 버림
    ("https://github.com/me/other", "me/other"),  # 7 커서 위면 새 수집기 몫
]


def _chrome(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
        "CREATE TABLE visits (id INTEGER PRIMARY KEY AUTOINCREMENT, url INTEGER NOT NULL,"
        " visit_time INTEGER NOT NULL, originator_cache_guid TEXT);"
    )
    for i, (url, title) in enumerate(PAGES):
        url_id = conn.execute("INSERT INTO urls (url, title) VALUES (?, ?)", (url, title)).lastrowid
        conn.execute(
            "INSERT INTO visits (url, visit_time, originator_cache_guid) VALUES (?, ?, '')",
            (url_id, to_webkit(T0 + timedelta(hours=i))),
        )
    conn.commit()
    conn.close()
    return path


def test_옛_규칙이_버리고_새_규칙이_남기는_방문만(tmp_path: Path) -> None:
    with snapshot(_chrome(tmp_path / "History")) as conn:
        found = revive.revive(conn, after_id=6, sent_above=[])
    assert [v.id for v in found] == [1, 5]


def test_커서_위에서_먼저_보낸_번호는_옛_수집기_몫이다(tmp_path: Path) -> None:
    # 6 은 커서 위라 새 수집기 몫이지만 어차피 버리는 주소.
    # 7 은 먼저 보낸 번호라 옛 규칙이 처리했다.
    with snapshot(_chrome(tmp_path / "History")) as conn:
        found = revive.revive(conn, after_id=5, sent_above=[7])
    assert [v.id for v in found] == [1, 5, 7]


def test_커서_위는_건드리지_않는다(tmp_path: Path) -> None:
    with snapshot(_chrome(tmp_path / "History")) as conn:
        found = revive.revive(conn, after_id=5, sent_above=[])
    assert 7 not in [v.id for v in found]


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "state"
    monkeypatch.setenv("JARVIS_STATE_DIR", str(folder))
    monkeypatch.setattr(revive, "chrome_loaded", lambda: False)
    return folder


def _outbox(folder: Path) -> List[str]:
    texts: List[str] = []
    for path in sorted((folder / "outbox").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            texts += [t["text"] for t in json.loads(line)["traces"]]
    return texts


def test_상태의_커서까지_되살려_상자에_적는다(tmp_path: Path, state: Path) -> None:
    history = _chrome(tmp_path / "History")
    save_state(STATE_NAME, {str(history): {"after_id": 6, "sent_above": []}})

    assert revive.main(["--profile", str(history)]) == 0
    assert _outbox(state) == ["me/repo (github.com)", "영상 (www.youtube.com)"]


def test_시험_실행은_상자에_쓰지_않는다(tmp_path: Path, state: Path) -> None:
    history = _chrome(tmp_path / "History")
    save_state(STATE_NAME, {str(history): {"after_id": 6, "sent_above": []}})

    assert revive.main(["--profile", str(history), "--dry-run"]) == 0
    assert _outbox(state) == []


def test_옛_크롬_수집기가_올라가_있으면_돌지_않는다(
    tmp_path: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 커서를 읽은 뒤 옛 코드가 한 번 더 돌면 그 사이 버린 방문이 아무 기록 없이 빠진다(재리뷰).
    monkeypatch.setattr(revive, "chrome_loaded", lambda: True)
    history = _chrome(tmp_path / "History")
    save_state(STATE_NAME, {str(history): {"after_id": 6, "sent_above": []}})

    assert revive.main(["--profile", str(history)]) == 1
    assert _outbox(state) == []


def test_커서가_없으면_돌지_않는다(tmp_path: Path, state: Path) -> None:
    history = _chrome(tmp_path / "History")
    assert revive.main(["--profile", str(history)]) == 1
    assert _outbox(state) == []
