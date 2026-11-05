"""관심사 준비 — 찾은 것만, 검색해서 쓴 것만 판단의 맥락에 넣는다."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

from src.brain.research import LABEL, TopicResearch
from src.brain.tools import Researched, Use
from src.core.models import Insight, Severity

NOW = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)
TOPIC = Insight(
    trigger="topic:62",
    summary="금융결제원 지원 — 뜨는 중",
    severity=Severity.NOTABLE,
    at=NOW,
    family="topic",
)


@dataclass
class Ran:
    result: Researched
    asked: List[List[Dict[str, Any]]] = field(default_factory=list)

    async def run(self, messages: List[Dict[str, Any]]) -> Researched:
        self.asked.append(messages)
        return self.result


SEARCHED = (Use("search", "금융결제원 채용 일정", True, "6개", 6),)


def _prepare(result: Researched) -> Any:
    tools = Ran(result)
    block = asyncio.run(TopicResearch(tools).prepare(TOPIC, NOW))  # type: ignore[arg-type]
    return block, tools


def test_what_was_found_goes_to_the_judge() -> None:
    block, tools = _prepare(Researched("· 2027 상반기 공고 접수 9/18~10/2 (잡코리아)", SEARCHED))
    assert block is not None and block.label == LABEL and "잡코리아" in block.body
    user = tools.asked[0][1]["content"]
    assert "관심사: 금융결제원 지원" in user and "2026-11-05" in user


def test_nothing_found_adds_nothing() -> None:
    block, _ = _prepare(Researched("없음", SEARCHED))
    assert block is None


def test_an_answer_written_without_searching_is_not_research() -> None:
    """모델이 아는 것만으로 쓴 \"찾아본 것\"은 찾아본 것이 아니다 — 일정을 지어낼 수 있다."""
    block, _ = _prepare(Researched("· 다음 주에 필기 시험이 있다"))
    assert block is None


def test_a_slow_research_gives_up_and_says_so(caplog: Any) -> None:
    """반응 루프 안에서 돈다

    — 그동안 수면·출근 신호도 기다린다(실측 173초). 넘기면 없이 판단한다.
    """
    from datetime import timedelta

    @dataclass
    class Slow:
        async def run(self, messages: List[Dict[str, Any]]) -> Researched:
            await asyncio.sleep(1)
            return Researched("늦은 답", SEARCHED)

    research = TopicResearch(Slow(), limit=timedelta(milliseconds=20))  # type: ignore[arg-type]
    assert asyncio.run(research.prepare(TOPIC, NOW)) is None
    assert "찾아본 것 없이 간다" in caplog.text


def test_interests_prefer_the_branch_and_the_busiest(tmp_path: Any) -> None:
    """검색어는 구체적일수록 낫다 — 줄기와 가지가 함께 오르면 가지만, 최근 근거가 많은 날 순."""
    from datetime import timedelta

    from src.brain.research import interests
    from src.core.models import Trace
    from src.core.topics import Op, plan
    from src.storage.topics import SQLiteTopicStore
    from src.storage.traces import SQLiteTraceStore

    db = tmp_path / "t.db"
    traces = SQLiteTraceStore(db)
    days = {"금결원": (0.5, 1.5, 2.5, 3.5), "여행": (0.5, 1.5, 2.5), "옛것": (40.5, 41.5)}
    traces.write(
        [
            Trace(
                source="mac_chrome", kind="web_visit", text=f"{name}{d}", at=NOW - timedelta(days=d)
            )
            for name, ds in days.items()
            for d in ds
        ]
    )
    ids = {t.text: t.id for t in traces.recent("web_visit", NOW - timedelta(days=60)) if t.id}
    pick = lambda name: tuple(i for text, i in ids.items() if text.startswith(name))  # noqa: E731
    topics = SQLiteTopicStore(db)
    ops = [
        Op("create", ref="a", name="취업", evidence=pick("금결원")),
        Op("create", ref="b", parent="a", name="금결원 지원", evidence=pick("금결원")),
        Op("create", name="삿포로 여행", evidence=pick("여행")),
        Op("create", name="옛 관심", evidence=pick("옛것")),
    ]
    topics.apply(plan(topics.load(), ops, set(ids.values()), NOW), NOW)

    assert interests(topics, NOW) == ["취업 › 금결원 지원", "삿포로 여행"]


def test_interests_take_the_branches_not_their_parent(tmp_path: Any) -> None:
    """줄기의 날 수에는 가지의 근거가 합쳐져 늘 가지 이상이다.

    날 수로만 고르면 줄기가 뽑히고 가지가 다 빠졌다(리뷰 재현: 가지 둘이 다른 날에 있으면 ['취업']).
    """
    from datetime import timedelta

    from src.brain.research import interests
    from src.core.models import Trace
    from src.core.topics import Op, plan
    from src.storage.topics import SQLiteTopicStore
    from src.storage.traces import SQLiteTraceStore

    db = tmp_path / "t.db"
    traces = SQLiteTraceStore(db)
    days = {"금결원": (0.5, 2.5), "은행": (1.5, 3.5)}
    traces.write(
        [
            Trace(
                source="mac_chrome", kind="web_visit", text=f"{name}{d}", at=NOW - timedelta(days=d)
            )
            for name, ds in days.items()
            for d in ds
        ]
    )
    ids = {t.text: t.id for t in traces.recent("web_visit", NOW - timedelta(days=10)) if t.id}
    pick = lambda name: tuple(i for text, i in ids.items() if text.startswith(name))  # noqa: E731
    topics = SQLiteTopicStore(db)
    ops = [
        Op("create", ref="a", name="취업", evidence=pick("금결원") + pick("은행")),
        Op("create", parent="a", name="금결원 지원", evidence=pick("금결원")),
        Op("create", parent="a", name="은행 채용", evidence=pick("은행")),
    ]
    topics.apply(plan(topics.load(), ops, set(ids.values()), NOW), NOW)

    assert sorted(interests(topics, NOW)) == ["취업 › 금결원 지원", "취업 › 은행 채용"]
