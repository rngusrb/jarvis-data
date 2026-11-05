"""관심사 신호 — 요즘 뜨는 주제만, 깨어 있는 시간에만, 같은 깨어남은 같은 신호로."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set

from src.core.clock import KST
from src.core.topics import Ground, Node, Tree, standings
from src.triggers.topics import TopicWatch, muted, topic_trigger

# 한국 시간 오후 3시 — 말 걸 시간대 안.
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=KST)


def _board(*topics: Sequence[float], old: Optional[Sequence[float]] = None) -> list:
    """주제마다 근거가 며칠 전에 있었나. `old` 는 1번 주제의 오래된 근거(쉬었다 깨어난 경우)."""
    nodes = {
        i: Node(i, f"주제{i}", None, NOW - timedelta(days=90)) for i in range(1, len(topics) + 1)
    }
    grounds: List[Ground] = []
    trace = 100
    for i, days in enumerate(topics, start=1):
        for d in list(days) + (list(old) if (old and i == 1) else []):
            trace += 1
            grounds.append(Ground(i, trace, NOW - timedelta(days=d), f"흔적{trace}"))
    tree = Tree(nodes=nodes, links={i: set() for i in nodes})
    return standings(tree, grounds, NOW)


def _check(board: list, now: datetime = NOW, quiet: Optional[Set[int]] = None) -> list:
    notes: Dict[Optional[int], List[str]] = {1: ["자격증 시험이 11월 말이다"]}
    return TopicWatch().check(board, notes, now, quiet or set())


def test_a_topic_rising_for_three_of_the_last_seven_days_is_a_candidate() -> None:
    [insight] = _check(_board([0.5, 1.5, 2.5]))
    assert insight.trigger == topic_trigger(1) == "topic:1"
    assert "요즘 뜨는 관심사 '주제1' — 최근 7일 중 3일" in insight.summary
    assert "내 메모: 자격증 시험이 11월 말이다" in insight.summary
    # 같은 깨어남이면 같은 신호다 — 게이트가 한 번만 판단한다.
    assert insight.at == NOW - timedelta(days=2.5)


def test_one_day_is_not_yet_a_trend() -> None:
    """하루뿐인 건 "처음 보임"이다 — 한 번 검색해본 걸 관심사라고 붙들지 않는다. 이틀이면 뜬다."""
    assert _check(_board([0.5, 0.6])) == []
    assert len(_check(_board([0.5, 1.5]))) == 1


def test_a_months_long_steady_topic_is_not_news() -> None:
    """몇 주째 이어진 주제는 이번 주에 부쩍 늘어도(뜨는 중) '새로' 뜬 게 아니다.

    깨어난 지 2주가 넘었다.
    """
    this_week = [d + 0.5 for d in range(7)]
    board = _board(this_week + [20.5, 35.5, 48.5])  # 사이가 28일을 안 넘어 한 번도 안 쉬었다
    assert board[0].trend.value == "뜨는 중"
    assert _check(board) == []


def test_a_topic_back_after_a_long_quiet_is_new_again() -> None:
    """잠잠(28일)했다가 다시 보이면 새 깨어남 — 그때부터 센다."""
    [insight] = _check(_board([0.5, 1.5, 2.5], old=[45, 46]))
    assert "요즘 다시 뜨는 관심사" in insight.summary
    assert insight.at == NOW - timedelta(days=2.5)


def test_stronger_and_newer_topics_come_first() -> None:
    insights = _check(_board([0.5, 1.5, 2.5], [0.5, 1.5, 2.5, 3.5]))
    assert [i.trigger for i in insights] == ["topic:2", "topic:1"]


def test_only_while_awake() -> None:
    """회고가 05:30 에 돌고 바로 다음 주기가 6시 전이다. 시간대가 없으면 자는 사람에게 간다."""
    board = _board([0.5, 1.5, 2.5])
    assert _check(board, now=datetime(2026, 11, 5, 5, 43, tzinfo=KST)) == []
    assert _check(board, now=datetime(2026, 11, 5, 21, 0, tzinfo=KST)) == []
    assert len(_check(board, now=datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc))) == 1  # 10시


def test_a_muted_topic_stays_quiet() -> None:
    assert _check(_board([0.5, 1.5, 2.5]), quiet={1}) == []


def test_muted_reads_only_thumbs_down_on_topics() -> None:
    reactions = [("topic:3", -1), ("topic:4", 1), ("late_sleep", -1), ("topic:x", -1)]
    assert muted(reactions) == {3}
