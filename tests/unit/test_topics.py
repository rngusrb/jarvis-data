"""주제 나무 — 연산 검사, 하이라이트, 오래 잠잠한 주제 접기.

저장소 없이 나무 사본 위에서만 본다. 모델이 무엇을 내든 나무가 깨지지 않는 게 요점이다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set

from src.core.topics import (
    RETIRE_AFTER,
    Ground,
    Node,
    Op,
    Tree,
    Trend,
    plan,
    stale,
    standings,
    trend,
)

NOW = datetime(2026, 11, 2, 20, 30, tzinfo=timezone.utc)
SHOWN: Set[int] = set(range(100, 200))


def _tree(*nodes: Node, links: Optional[Dict[int, Set[int]]] = None) -> Tree:
    links = links or {}
    return Tree(
        nodes={n.id: n for n in nodes},
        links={n.id: set(links.get(n.id, set())) for n in nodes},
        version=7,
    )


def _node(id: int, name: str, parent: Optional[int] = None) -> Node:
    return Node(id=id, name=name, parent=parent, created_at=NOW - timedelta(days=30))


def _days_ago(*days: float) -> List[datetime]:
    return [NOW - timedelta(days=d) for d in days]


# ── 만들기·붙이기 ─────────────────────────────────────


def test_creates_a_branch_under_a_branch_made_in_the_same_reply() -> None:
    p = plan(
        _tree(),
        [
            Op("create", ref="a", name="취업", evidence=(100, 101)),
            Op("create", ref="b", name="자소서", parent="a", evidence=(102, 103)),
        ],
        SHOWN,
        NOW,
    )
    assert p.rejected == []
    trunk = next(n for n in p.after.live() if n.name == "취업")
    branch = next(n for n in p.after.live() if n.name == "자소서")
    assert branch.parent == trunk.id and trunk.id < 0
    assert [c.op for c in p.changes] == ["create", "create"]


def test_a_new_topic_needs_two_different_traces() -> None:
    """흔적 하나짜리는 관심사가 아니라 스쳐간 호기심이다. 같은 번호 두 번은 하나다."""
    p = plan(_tree(), [Op("create", name="주거", evidence=(100, 100))], SHOWN, NOW)
    assert p.after.live() == []
    assert "근거가 1개뿐" in p.rejected[0]


def test_evidence_must_be_a_trace_that_was_shown() -> None:
    """모델이 지어낸 번호나 보지도 않은 흔적이 근거가 되면 출처 추적이 깨진다."""
    p = plan(_tree(), [Op("create", name="주거", evidence=(100, 999, 998))], SHOWN, NOW)
    assert p.after.live() == []
    assert "근거가 1개뿐" in p.rejected[0]
    assert "보여주지 않은 흔적 번호 2개는 뺐다" in p.notices[0]


def test_same_name_in_the_same_place_attaches_instead_of_creating() -> None:
    """옛 사고: 같은 kind 를 한 응답에 네 번 내 값과 근거의 짝이 어긋났다."""
    tree = _tree(_node(1, "취업"), links={1: {100}})
    p = plan(tree, [Op("create", ref="x", name=" 취 업 ", evidence=(101,))], SHOWN, NOW)
    assert [n.id for n in p.after.live()] == [1]
    assert p.after.links[1] == {100, 101}
    assert "#1 와 이름이 같아 거기 붙였다" in p.notices[0]


def test_a_branch_takes_the_evidence_down_from_its_trunk() -> None:
    """가장 구체적인 주제에 붙는다. 줄기의 같은 연결은 닫는다.

    지우지 않는다 — 저장소가 뗀 때를 적는다.
    """
    tree = _tree(_node(1, "취업"), links={1: {100, 101, 102}})
    p = plan(tree, [Op("create", name="자소서", parent=1, evidence=(100, 101))], SHOWN, NOW)
    branch = next(n for n in p.after.live() if n.name == "자소서")
    assert p.after.links[branch.id] == {100, 101}
    assert p.after.links[1] == {102}
    # 원래 나무는 그대로다 — 저장소가 둘을 견줘 차이를 쓴다.
    assert tree.links[1] == {100, 101, 102}


def test_attaching_to_a_trunk_what_a_branch_holds_is_a_no_op() -> None:
    tree = _tree(_node(1, "취업"), _node(2, "자소서", 1), links={2: {100}})
    p = plan(tree, [Op("attach", topic=1, evidence=(100,))], SHOWN, NOW)
    assert p.after.links[1] == set() and p.changes == []


def test_ids_as_text_or_with_a_hash_are_ids() -> None:
    """모델이 ref 를 숫자로 지어도, 있는 주제 번호와 안 겹치면 ref 로 본다."""
    p = plan(
        _tree(_node(1, "취업")),
        [
            Op("create", ref="7", name="여행", evidence=(100, 101)),
            Op("note", topic=7, text="삿포로"),
        ],
        SHOWN,
        NOW,
    )
    assert p.rejected == [] and p.notes[0].topic is not None and p.notes[0].topic < 0


# ── 모양 바꾸기 ───────────────────────────────────────


def test_reshaping_needs_a_reason() -> None:
    tree = _tree(_node(1, "취업"), _node(2, "여행"))
    ops = [
        Op("rename", topic=1, name="구직"),
        Op("move", topic=2, parent=1),
        Op("merge", topic=2, into=1),
        Op("retire", topic=2),
    ]
    p = plan(tree, ops, SHOWN, NOW)
    assert len(p.rejected) == 4 and all("이유가 없다" in r for r in p.rejected)
    assert p.changes == []


def test_cannot_move_a_topic_under_its_own_branch() -> None:
    tree = _tree(_node(1, "취업"), _node(2, "자소서", 1))
    p = plan(tree, [Op("move", topic=1, parent=2, reason="r")], SHOWN, NOW)
    assert "자기 아래로는 옮길 수 없다" in p.rejected[0]
    assert p.after.nodes[1].parent is None


def test_moving_to_the_top_promotes_to_a_trunk() -> None:
    tree = _tree(_node(1, "취업"), _node(2, "코딩", 1))
    p = plan(tree, [Op("move", topic=2, parent=None, reason="취업과 따로 공부한다")], SHOWN, NOW)
    assert p.after.nodes[2].parent is None
    assert p.changes[0].detail == {"from": 1, "to": None}


def test_merge_moves_links_and_branches_and_retires() -> None:
    tree = _tree(
        _node(1, "취업"),
        _node(2, "구직"),
        _node(3, "면접", 2),
        links={2: {100, 101}, 1: {101, 102}},
    )
    p = plan(tree, [Op("merge", topic=2, into=1, reason="같은 것")], SHOWN, NOW)
    assert p.after.nodes[2].retired_at == NOW
    assert p.after.nodes[3].parent == 1
    assert p.after.links[1] == {100, 101, 102} and p.after.links[2] == set()
    assert p.changes[0].detail == {"into": 1, "evidence": [100], "children": [3]}


def test_cannot_merge_into_its_own_branch() -> None:
    tree = _tree(_node(1, "취업"), _node(2, "자소서", 1))
    p = plan(tree, [Op("merge", topic=1, into=2, reason="r")], SHOWN, NOW)
    assert "자기 아래 주제로는 합칠 수 없다" in p.rejected[0]


def test_retiring_a_topic_with_live_branches_is_refused() -> None:
    tree = _tree(_node(1, "취업"), _node(2, "자소서", 1))
    p = plan(tree, [Op("retire", topic=1, reason="r")], SHOWN, NOW)
    assert "아래에 살아 있는 주제가 1개" in p.rejected[0]
    # 가지부터 접으면 된다(차례대로 검사한다).
    p = plan(
        tree, [Op("retire", topic=2, reason="r"), Op("retire", topic=1, reason="r")], SHOWN, NOW
    )
    assert p.rejected == [] and p.after.live() == []


def test_a_retired_topic_cannot_be_touched() -> None:
    gone = _node(1, "야구")
    gone.retired_at = NOW - timedelta(days=1)
    p = plan(_tree(gone), [Op("attach", topic=1, evidence=(100,))], SHOWN, NOW)
    assert "접힌 주제다" in p.rejected[0]


def test_a_rejected_create_takes_its_dependents_down() -> None:
    """뒤 연산이 빠진 연산의 ref 를 가리키면 "모르는 ref" 로 같이 빠진다."""
    p = plan(
        _tree(),
        [
            Op("create", ref="a", name="취업", evidence=(100,)),
            Op("create", name="자소서", parent="a", evidence=(101, 102)),
            Op("create", name="여행", evidence=(103, 104)),
        ],
        SHOWN,
        NOW,
    )
    assert [n.name for n in p.after.live()] == ["여행"]
    assert "모르는 ref 'a'" in p.rejected[1]


def test_unknown_ops_are_rejected_not_ignored() -> None:
    p = plan(_tree(_node(1, "취업")), [Op("delete", topic=1)], SHOWN, NOW)
    assert "모르는 연산 'delete'" in p.rejected[0]


# ── 하이라이트 ───────────────────────────────────────


def test_trend_by_days_with_evidence() -> None:
    assert trend([], NOW) is Trend.QUIET
    assert trend(_days_ago(0.1, 0.2, 0.3), NOW) is Trend.NEW  # 하루뿐 — 옛 "후보"
    assert trend(_days_ago(1, 3), NOW) is Trend.RISING  # 새로 이틀
    # 주 2일 → 주 2일: 꾸준. 한 번에 쏟아진 검색은 하루로 센다.
    steady = _days_ago(1, 4, *[4.1] * 20, 8, 11, 15, 18, 22, 25)
    assert trend(steady, NOW) is Trend.STEADY
    assert trend(_days_ago(2, 8, 10, 15, 18, 22, 25), NOW) is Trend.FADING  # 주 2일 → 1일
    assert trend(_days_ago(9, 12), NOW) is Trend.FADING  # 최근 7일엔 없음
    assert trend(_days_ago(30, 40), NOW) is Trend.QUIET


def test_standings_count_branch_evidence_once_in_the_trunk() -> None:
    tree = _tree(_node(1, "취업"), _node(2, "자소서", 1), links={1: {100}, 2: {101, 102}})
    grounds = [
        Ground(1, 100, NOW - timedelta(days=1), "채용 공고"),
        Ground(2, 101, NOW - timedelta(days=1), "자소서 항목"),
        Ground(2, 102, NOW - timedelta(days=3), "자소서 예시"),
    ]
    board = standings(tree, grounds, NOW)
    assert [(s.path, s.depth, s.count, s.days) for s in board] == [
        (("취업",), 0, 3, 2),
        (("취업", "자소서"), 1, 2, 2),
    ]
    assert [g.trace for g in board[0].own] == [100]


# ── 오래 잠잠한 주제 ─────────────────────────────────


def _grounds(topic: int, ages: Sequence[float]) -> List[Ground]:
    return [Ground(topic, 100 + i, NOW - timedelta(days=a), "x") for i, a in enumerate(ages)]


def test_stale_retires_long_quiet_topics_from_the_leaves_up() -> None:
    tree = _tree(_node(1, "야구"), _node(2, "직관", 1), _node(3, "취업"))
    old = RETIRE_AFTER.days + 1
    grounds = _grounds(2, [old]) + _grounds(3, [1])
    ops = stale(tree, grounds, NOW)
    assert [op.topic for op in ops] == [2, 1]
    p = plan(tree, ops, set(), NOW)
    assert p.rejected == [] and [n.name for n in p.after.live()] == ["취업"]
    assert p.changes[0].reason == f"{RETIRE_AFTER.days}일 동안 근거가 없다"


def test_a_trunk_stays_while_a_branch_is_alive() -> None:
    tree = _tree(_node(1, "취업"), _node(2, "자소서", 1))
    grounds = _grounds(1, [RETIRE_AFTER.days + 5]) + _grounds(2, [3])
    assert stale(tree, grounds, NOW) == []
