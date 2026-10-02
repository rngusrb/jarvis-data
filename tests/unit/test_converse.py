"""대화 응답 — 싼 길과 비싼 길.

명령어는 LLM 을 안 불러야 한다. "지금 뭘 알고 있나"는 자비스가 생각할
필요가 없는 질문이고, 3090 을 데우는 동안 기다릴 이유도 없다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from src.brain.converse import Conversationalist
from src.channels.base import Incoming
from src.core.beliefs import Belief
from src.core.models import Observation
from src.storage.beliefs import SQLiteBeliefStore
from src.storage.conversation import SQLiteConversation
from src.storage.sqlite import SQLiteStore

NOW = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)


@dataclass
class CountingReasoner:
    calls: int = 0
    answer: str = "요즘 잠이 좀 부족해 보여."
    prompts: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return self.answer


def _build(tmp_path: Path, reasoner: Optional[CountingReasoner] = None) -> Conversationalist:
    db = tmp_path / "t.db"
    store = SQLiteStore(db)
    store.write(
        [
            Observation(
                source="apple_health",
                kind="sleep_hours",
                value=v,
                at=NOW - timedelta(days=d),
                meta={"segments": 16},
            )
            for d, v in ((1, 5.16), (0, 3.06))
        ]
    )
    return Conversationalist(
        reasoner=reasoner or CountingReasoner(),
        beliefs=SQLiteBeliefStore(db),
        catalog=store,
        source=store,
        history=SQLiteConversation(db),
    )


def _ask(c: Conversationalist, text: str) -> Optional[str]:
    return asyncio.run(c.reply(Incoming(text=text, at=NOW, cursor=1), NOW))


def test_명령어는_llm을_부르지_않는다(tmp_path: Path) -> None:
    reasoner = CountingReasoner()
    c = _build(tmp_path, reasoner)
    for command in ("/status", "/sleep", "/beliefs", "/help"):
        assert _ask(c, command)
    assert reasoner.calls == 0


def test_sleep_명령이_실제_값을_보여준다(tmp_path: Path) -> None:
    answer = _ask(_build(tmp_path), "/sleep")
    assert answer is not None
    assert "3.1시간" in answer or "3.1" in answer
    assert "평균" in answer


def test_status_는_끊긴_것을_표시한다(tmp_path: Path) -> None:
    answer = _ask(_build(tmp_path), "/status")
    assert answer is not None and "sleep_hours" in answer


def test_아는_게_없으면_없다고_한다(tmp_path: Path) -> None:
    answer = _ask(_build(tmp_path), "/beliefs")
    assert answer is not None and "아직 아는 게 없어" in answer


def test_자유_질문은_llm을_부른다(tmp_path: Path) -> None:
    reasoner = CountingReasoner()
    answer = _ask(_build(tmp_path, reasoner), "요즘 나 어때?")
    assert answer == "요즘 잠이 좀 부족해 보여."
    assert reasoner.calls == 1


def test_믿음이_프롬프트에_들어간다(tmp_path: Path) -> None:
    reasoner = CountingReasoner()
    c = _build(tmp_path, reasoner)
    for at in (NOW - timedelta(days=2), NOW - timedelta(days=1)):
        c.beliefs.observe(
            Belief(
                kind="관심사:채용준비",
                value="AI 직무 지원 중",
                confidence=0.9,
                first_seen=at,
                last_seen=at,
                evidence=("토스 채용", "KT 대졸신입"),
            ),
            at,
        )
    _ask(c, "나 요즘 뭐 하고 있지?")
    assert "관심사:채용준비" in reasoner.prompts[-1]


def test_빈_응답을_그대로_돌려주지_않는다(tmp_path: Path) -> None:
    """빈 문자열을 보내면 사용자는 아무 일도 안 일어난 걸로 본다."""
    answer = _ask(_build(tmp_path, CountingReasoner(answer="   ")), "뭐해?")
    assert answer is not None and answer.strip()


def test_아직_안_붙은_버튼은_알려준다(tmp_path: Path) -> None:
    c = _build(tmp_path)
    answer = asyncio.run(
        c.reply(Incoming(text="x", at=NOW, cursor=1, action="useless:sleep_drop"), NOW)
    )
    assert answer is not None and "아직" in answer
