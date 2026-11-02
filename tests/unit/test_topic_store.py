"""주제 나무 저장소 — 계획을 한 트랜잭션에, 지우지 않고, 낡은 나무 위의 계획은 거절."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

import pytest

from src.core.models import Trace
from src.core.topics import CODE, MODEL, Op, plan
from src.storage.topics import Run, SQLiteTopicStore, StaleTree
from src.storage.traces import SQLiteTraceStore

NOW = datetime(2026, 11, 2, 20, 30, tzinfo=timezone.utc)


def _store(tmp_path: Path, texts: List[str]) -> Tuple[SQLiteTopicStore, List[int]]:
    db = tmp_path / "t.db"
    traces = SQLiteTraceStore(db)
    traces.write(
        [
            Trace(source="mac_chrome", kind="web_visit", text=t, at=NOW - timedelta(hours=i))
            for i, t in enumerate(texts)
        ]
    )
    ids = [t.id for t in traces.recent("web_visit", NOW - timedelta(days=1)) if t.id is not None]
    return SQLiteTopicStore(db), sorted(ids)


def _rows(tmp_path: Path, sql: str) -> list:
    conn = sqlite3.connect(tmp_path / "t.db")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_a_plan_lands_with_real_ids_parents_and_change_records(tmp_path: Path) -> None:
    store, ids = _store(tmp_path, ["채용 공고", "자소서 항목", "자소서 예시", "면접 후기"])
    ops = [
        Op("create", ref="a", name="취업", evidence=(ids[0], ids[3]), reason="공고와 면접"),
        Op("create", ref="b", name="자소서", parent="a", evidence=(ids[1], ids[2])),
        Op("note", topic="b", text="금융권 위주"),
    ]
    p = plan(store.load(), ops, set(ids), NOW)
    mapped = store.apply(p, NOW)

    tree = store.load()
    trunk, branch = tree.live()
    assert (trunk.name, branch.name, branch.parent) == ("취업", "자소서", trunk.id)
    assert sorted(mapped.values()) == [trunk.id, branch.id]
    assert tree.links[branch.id] == {ids[1], ids[2]}
    assert [c.op for c in store.changes()] == ["create", "create"]
    assert store.changes()[1].detail["parent"] == trunk.id  # 임시 번호가 아니라 진짜 번호
    assert store.latest_notes(2)[branch.id][0].text == "금융권 위주"
    assert {g.text for g in store.grounds() if g.topic == trunk.id} == {"채용 공고", "면접 후기"}


def test_a_branch_moved_under_a_later_branch_still_lands(tmp_path: Path) -> None:
    """한 응답 안에서 먼저 만든 주제를 뒤에 만든 주제 아래로 옮길 수 있다."""
    store, ids = _store(tmp_path, ["a", "b", "c", "d"])
    ops = [
        Op("create", ref="x", name="자소서", evidence=(ids[0], ids[1])),
        Op("create", ref="y", name="취업", evidence=(ids[2], ids[3])),
        Op("move", topic="x", parent="y", reason="자소서는 취업의 일부"),
    ]
    store.apply(plan(store.load(), ops, set(ids), NOW), NOW)
    tree = store.load()
    by_name = {n.name: n for n in tree.live()}
    assert by_name["자소서"].parent == by_name["취업"].id


def test_moving_evidence_closes_the_old_link_instead_of_deleting_it(tmp_path: Path) -> None:
    """근거는 옮기지 않고 연결만 바꾼다. 옛 연결도 지우지 않는다."""
    store, ids = _store(tmp_path, ["a", "b", "c"])
    store.apply(
        plan(store.load(), [Op("create", name="취업", evidence=tuple(ids))], set(ids), NOW), NOW
    )
    trunk = store.load().live()[0].id
    later = NOW + timedelta(days=1)
    split = [Op("create", name="자소서", parent=trunk, evidence=(ids[0], ids[1]))]
    store.apply(plan(store.load(), split, set(ids), later), later)

    rows = _rows(
        tmp_path, f"SELECT trace_id, removed_at FROM topic_evidence WHERE topic_id = {trunk}"
    )
    closed = sorted(t for t, removed in rows if removed)
    assert closed == [ids[0], ids[1]] and len(rows) == 3
    assert store.load().links[trunk] == {ids[2]}


def test_merge_and_retire_keep_the_rows(tmp_path: Path) -> None:
    store, ids = _store(tmp_path, ["a", "b", "c", "d"])
    ops = [
        Op("create", name="취업", evidence=(ids[0], ids[1])),
        Op("create", name="구직", evidence=(ids[2], ids[3])),
    ]
    store.apply(plan(store.load(), ops, set(ids), NOW), NOW)
    first, second = (n.id for n in store.load().live())
    store.apply(
        plan(store.load(), [Op("merge", topic=second, into=first, reason="같은 것")], set(), NOW),
        NOW,
    )

    tree = store.load()
    assert [n.id for n in tree.live()] == [first]
    assert tree.nodes[second].retired_at is not None  # 지우지 않고 접는다
    assert tree.links[first] == set(ids)
    assert store.changes()[-1].reason == "같은 것"


def test_a_plan_made_on_a_stale_tree_is_refused(tmp_path: Path) -> None:
    """읽은 뒤 다른 회고가 나무를 바꿨으면 그 위에서 짠 계획은 적용하지 않는다(시끄럽게)."""
    store, ids = _store(tmp_path, ["a", "b", "c", "d"])
    seen = store.load()
    store.apply(
        plan(seen, [Op("create", name="취업", evidence=(ids[0], ids[1]))], set(ids), NOW), NOW
    )
    late = plan(seen, [Op("create", name="여행", evidence=(ids[2], ids[3]))], set(ids), NOW)
    with pytest.raises(StaleTree):
        store.apply(late, NOW)
    assert [n.name for n in store.load().live()] == ["취업"]


def test_nothing_is_written_when_one_write_fails(tmp_path: Path) -> None:
    """한 회고의 변경은 전부 적용되거나 하나도 안 된다."""
    store, ids = _store(tmp_path, ["a", "b"])
    p = plan(store.load(), [Op("create", name="취업", evidence=tuple(ids))], set(ids), NOW)
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON topic_changes BEGIN SELECT RAISE(ABORT, 'x'); END"
    )
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        store.apply(p, NOW)
    assert _rows(tmp_path, "SELECT COUNT(*) FROM topics") == [(0,)]
    assert _rows(tmp_path, "SELECT COUNT(*) FROM topic_evidence") == [(0,)]


def test_last_run_counts_only_runs_that_did_not_fail(tmp_path: Path) -> None:
    store, _ = _store(tmp_path, [])
    assert store.last_run() is None
    store.record_run(Run(NOW, NOW, "ok", considered=10, changed=3, notices=2))
    store.record_run(Run(NOW + timedelta(days=1), NOW, "failed: 응답을 못 읽었다"))
    assert store.last_run() == NOW
    assert _rows(tmp_path, "SELECT considered, changed, notices FROM reflections LIMIT 1") == [
        (10, 3, 2)
    ]


def test_changes_remember_who_made_them(tmp_path: Path) -> None:
    """코드가 접은 것과 모델이 바꾼 것을 가려야 변경 빈도를 잴 수 있다."""
    store, ids = _store(tmp_path, ["a", "b"])
    store.apply(
        plan(store.load(), [Op("create", name="야구", evidence=tuple(ids))], set(ids), NOW), NOW
    )
    topic = store.load().live()[0].id
    retire = [Op("retire", topic=topic, reason="61일 동안 근거가 없다")]
    store.apply(plan(store.load(), retire, set(), NOW, actor=CODE), NOW)
    assert [(c.op, c.actor) for c in store.changes()] == [("create", MODEL), ("retire", CODE)]
