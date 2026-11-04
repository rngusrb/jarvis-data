"""회고 되감기 — 3단계 끝 기준("나무가 너무 자주 바뀌지 않는지")을 지난 기록으로 잰다.

숫자가 결정의 근거가 되므로 무엇을 세는지가 중요하다. 코드가 접은 것을 모델의 구조 변경으로
세면 변경 빈도가 부풀려진다(JAR-022 리뷰 재현).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pytest

from app.main import REFLECT_AT
from app.reflect import replay
from src.brain.reflect import Reflector
from src.core.clock import KST
from src.core.models import Trace
from src.core.traces import TraceKind
from src.storage.topics import SQLiteTopicStore
from src.storage.traces import SQLiteTraceStore

START = date(2026, 8, 8)
WEB = TraceKind(kind="web_visit", label="웹 방문", collector="mac_chrome")


@dataclass
class FirstDayOnly:
    """첫날만 주제를 하나 만들고, 그 뒤로는 바꿀 게 없다고 답한다."""

    calls: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.calls.append(prompt)
        if len(self.calls) == 1:
            return '{"ops": [{"op": "create", "name": "야구", "evidence": [1, 2]}]}'
        return '{"ops": []}'


def _reflector(tmp_path: Path) -> Reflector:
    db = tmp_path / "t.db"
    traces = SQLiteTraceStore(db)
    morning = datetime.combine(START, REFLECT_AT, tzinfo=KST)
    traces.write(
        [
            Trace(source="mac_chrome", kind="web_visit", text=t, at=morning - timedelta(hours=h))
            for h, t in ((3, "야구 중계"), (2, "야구 순위"))
        ]
    )
    return Reflector(
        reasoner=FirstDayOnly(), traces=traces, topics=SQLiteTopicStore(db), kinds=[WEB]
    )


def test_auto_retire_is_not_counted_as_the_models_reshaping(tmp_path: Path) -> None:
    """모델은 첫날 하나 만들고 매일 바꿀 게 없다고 했다. 61일째 코드가 접은 것은 따로 센다."""
    reflector = _reflector(tmp_path)
    end = START + timedelta(days=62)

    found = asyncio.run(replay(reflector, START, end, verbose=False))

    assert found.ops == {"create": 1}
    assert found.auto_retired == 1
    assert found.reshaped_days == 0 and found.days == 63


def test_refuses_a_tree_that_already_grew_past_the_start(tmp_path: Path) -> None:
    """운영 DB 나 한 번 되감은 사본에서 다시 돌면 '지금'보다 뒤의 근거가 섞인다."""
    reflector = _reflector(tmp_path)
    asyncio.run(replay(reflector, START, START + timedelta(days=1), verbose=False))
    with pytest.raises(SystemExit):
        asyncio.run(replay(reflector, START, START + timedelta(days=1), verbose=False))
