"""듣는 루프 — 커서 전진과 실패 격리."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from src.channels.base import Incoming
from src.runtime.listen import ListenLoop
from src.storage.conversation import SQLiteConversation

NOW = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)


@dataclass
class ScriptedSource:
    batches: List[List[Incoming]]
    seen_since: List[int] = field(default_factory=list)

    async def receive(self, since: int) -> List[Incoming]:
        self.seen_since.append(since)
        return self.batches.pop(0) if self.batches else []


@dataclass
class EchoResponder:
    calls: int = 0

    async def reply(self, incoming: Incoming, now: datetime) -> Optional[str]:
        self.calls += 1
        return f"받았어: {incoming.text}"


@dataclass
class RecordingSpeaker:
    sent: List[str] = field(default_factory=list)

    async def send(self, text: str) -> None:
        self.sent.append(text)


def _incoming(cursor: int, text: str) -> Incoming:
    return Incoming(text=text, at=NOW + timedelta(seconds=cursor), cursor=cursor)


def test_주고받은_말이_저장된다(tmp_path: Path) -> None:
    log = SQLiteConversation(tmp_path / "t.db")
    speaker = RecordingSpeaker()
    loop = ListenLoop(
        source=ScriptedSource([[_incoming(1, "안녕")]]),
        responder=EchoResponder(),
        speaker=speaker,
        log=log,
    )
    assert asyncio.run(loop.run_once()) == 1
    assert speaker.sent == ["받았어: 안녕"]
    assert [(m.who, m.text) for m in log.recent()] == [
        ("user", "안녕"),
        ("jarvis", "받았어: 안녕"),
    ]


def test_커서가_다음_호출에_쓰인다(tmp_path: Path) -> None:
    """저장을 빼먹으면 재시작할 때마다 하루치를 다시 읽는다."""
    source = ScriptedSource([[_incoming(4, "하나")], [_incoming(9, "둘")]])
    loop = ListenLoop(
        source=source,
        responder=EchoResponder(),
        speaker=RecordingSpeaker(),
        log=SQLiteConversation(tmp_path / "t.db"),
    )
    asyncio.run(loop.run_once())
    asyncio.run(loop.run_once())
    assert source.seen_since == [0, 4]


def test_커서는_뒤로_가지_않는다(tmp_path: Path) -> None:
    log = SQLiteConversation(tmp_path / "t.db")
    log.advance("telegram", 10)
    log.advance("telegram", 3)
    assert log.cursor("telegram") == 10


def test_답_만들기가_터져도_커서는_넘어간다(tmp_path: Path) -> None:
    """처리하다 터진 메시지를 다시 읽으면 영원히 같은 자리에서 터진다.

    한 건을 잃는 쪽이 무한 반복보다 낫다.
    """

    @dataclass
    class BrokenResponder:
        async def reply(self, incoming: Incoming, now: datetime) -> Optional[str]:
            raise RuntimeError("두뇌가 죽었다")

    log = SQLiteConversation(tmp_path / "t.db")
    loop = ListenLoop(
        source=ScriptedSource([[_incoming(3, "질문")]]),
        responder=BrokenResponder(),
        speaker=RecordingSpeaker(),
        log=log,
    )
    assert asyncio.run(loop.run_once()) == 0
    assert log.cursor("telegram") == 3


def test_듣기_실패가_루프를_죽이지_않는다(tmp_path: Path) -> None:
    @dataclass
    class BrokenSource:
        async def receive(self, since: int) -> List[Incoming]:
            raise ConnectionError("네트워크")

    loop = ListenLoop(
        source=BrokenSource(),
        responder=EchoResponder(),
        speaker=RecordingSpeaker(),
        log=SQLiteConversation(tmp_path / "t.db"),
    )
    assert asyncio.run(loop.run_once()) == 0
