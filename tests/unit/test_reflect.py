"""회고 엔진.

LLM 응답은 가짜로 두고 **그 주변의 규율**만 본다 — 근거 강제, 이름 난장판
방지, 시든 믿음을 프롬프트에서 빼는 것.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from src.brain.reflect import Reflector, _parse
from src.core.beliefs import FORGET_AFTER, Belief
from src.core.models import Trace
from src.core.traces import TraceKind
from src.storage.beliefs import SQLiteBeliefStore
from src.storage.traces import SQLiteTraceStore

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
WEB = TraceKind(kind="web_visit", label="웹 방문", collector="mac_chrome")


@dataclass
class ScriptedReasoner:
    reply: str
    prompts: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.prompts.append(prompt)
        return self.reply


def _build(tmp_path: Path, reply: str) -> Reflector:
    db = tmp_path / "t.db"
    traces = SQLiteTraceStore(db)
    traces.write(
        [
            Trace(source="mac_chrome", kind="web_visit", text=t, at=NOW - timedelta(hours=i))
            for i, t in enumerate(["검색: 전세자금대출 금리", "검색: 버팀목 자격"])
        ]
    )
    return Reflector(
        reasoner=ScriptedReasoner(reply),
        traces=traces,
        beliefs=SQLiteBeliefStore(db),
        kinds=[WEB],
    )


GOOD = """[
  {"kind": "관심사:주거", "value": "전세자금대출을 알아보는 중",
   "confidence": 0.8,
   "evidence": ["검색: 전세자금대출 금리", "검색: 버팀목 자격"]}
]"""


def test_learns_a_belief_from_traces(tmp_path: Path) -> None:
    result = asyncio.run(_build(tmp_path, GOOD).run_once(NOW))
    assert result.considered == 2
    assert [b.kind for b in result.learned] == ["관심사:주거"]


def test_rejects_a_belief_without_enough_evidence(tmp_path: Path) -> None:
    """근거를 실제보다 세게 말하는 게 이 방식의 주된 실패 모양이다."""
    thin = '[{"kind": "관심사:주거", "value": "이사 준비 중", "confidence": 0.9, "evidence": []}]'
    result = asyncio.run(_build(tmp_path, thin).run_once(NOW))
    assert result.learned == []
    assert "근거가 0개" in result.skipped[0]


def test_shows_existing_kinds_to_prevent_name_sprawl(tmp_path: Path) -> None:
    """기존 목록을 안 보여주면 같은 걸 매번 새 이름으로 만든다."""
    reflector = _build(tmp_path, GOOD)
    asyncio.run(reflector.run_once(NOW))
    asyncio.run(reflector.run_once(NOW + timedelta(days=1)))

    reasoner = reflector.reasoner
    assert isinstance(reasoner, ScriptedReasoner)
    assert "관심사:주거" in reasoner.prompts[-1]
    assert "위 kind를 그대로 써라" in reasoner.prompts[-1]


def test_forgets_before_it_prompts(tmp_path: Path) -> None:
    """시든 믿음이 프롬프트에 남으면 모델이 그걸 근거로 스스로 되살린다."""
    reflector = _build(tmp_path, "[]")
    reflector.beliefs.observe(
        Belief(
            kind="관심사:죽은것",
            value="옛날 관심사",
            confidence=0.9,
            first_seen=NOW,
            last_seen=NOW,
            evidence=("옛날 검색",),
        ),
        NOW,
    )
    much_later = NOW + FORGET_AFTER + timedelta(days=1)
    # 그때도 볼 흔적이 있어야 프롬프트가 만들어진다. 흔적이 없으면 회고는
    # 모델을 부르지도 않는데, 그건 이 테스트가 보려는 게 아니다.
    reflector.traces.write(
        [Trace(source="mac_chrome", kind="web_visit", text="검색: 요즘 것", at=much_later)]
    )
    result = asyncio.run(reflector.run_once(much_later))

    assert result.forgotten == ["관심사:죽은것"]
    reasoner = reflector.reasoner
    assert isinstance(reasoner, ScriptedReasoner)
    assert "관심사:죽은것" not in reasoner.prompts[-1]


def test_parse_tolerates_fences_and_chatter() -> None:
    """완벽한 형식을 요구하는 것보다 관대하게 읽는 쪽이 실제로 잘 돈다."""
    messy = '알겠습니다.\n```json\n[{"kind": "a", "value": "b"}]\n```\n이상입니다.'
    assert _parse(messy) == [{"kind": "a", "value": "b"}]
    assert _parse("설명만 하고 배열이 없음") == []
    assert _parse("") == []
