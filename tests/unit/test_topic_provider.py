"""요즘 관심사 — 주제 나무에서 뜨는 중·꾸준한 것만 맥락에."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

from src.brain.providers import TopicProvider
from src.core.models import Trace
from src.core.topics import Op, plan
from src.storage.topics import SQLiteTopicStore
from src.storage.traces import SQLiteTraceStore

NOW = datetime(2026, 11, 2, 20, 30, tzinfo=timezone.utc)


def _store(tmp_path: Path, days: List[float]) -> Tuple[SQLiteTopicStore, List[int]]:
    db = tmp_path / "t.db"
    traces = SQLiteTraceStore(db)
    traces.write(
        [
            Trace(
                source="mac_chrome", kind="web_visit", text=f"흔적 {i}", at=NOW - timedelta(days=d)
            )
            for i, d in enumerate(days)
        ]
    )
    found = traces.recent("web_visit", NOW - timedelta(days=60))
    # 쓴 차례대로 — days 의 순서와 같다.
    return SQLiteTopicStore(db), sorted(t.id for t in found if t.id is not None)


def test_shows_rising_topics_with_their_path_and_latest_note(tmp_path: Path) -> None:
    store, ids = _store(tmp_path, [1, 3, 0.5, 2])
    ops = [
        Op("create", ref="a", name="취업", evidence=(ids[0], ids[1])),
        Op("create", ref="b", name="자소서", parent="a", evidence=(ids[2], ids[3])),
        Op("note", topic="b", text="금융권 위주"),
    ]
    store.apply(plan(store.load(), ops, set(ids), NOW), NOW)

    block = TopicProvider(topics=store).fetch(None, NOW)

    assert block is not None
    assert block.body.splitlines() == [
        "- 취업 — 뜨는 중(최근 7일 중 4일)",
        "- 취업 › 자소서 — 뜨는 중(최근 7일 중 2일): 금융권 위주",
    ]


def test_leaves_out_what_was_seen_on_one_day_only(tmp_path: Path) -> None:
    """한 번 검색해본 것까지 관심사라고 넣으면 스쳐간 호기심을 붙들고 늘어진다."""
    store, ids = _store(tmp_path, [1, 1.1, 40, 41])
    ops = [
        Op("create", name="삿포로 여행", evidence=(ids[0], ids[1])),
        Op("create", name="야구", evidence=(ids[2], ids[3])),
    ]
    store.apply(plan(store.load(), ops, set(ids), NOW), NOW)
    assert TopicProvider(topics=store).fetch(None, NOW) is None
