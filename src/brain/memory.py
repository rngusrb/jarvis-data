"""자비스가 "언제 무슨 말을 했는지" 기억하는 곳.

사람으로 치면 대화 기억이다. 이게 없으면 자비스는 30분마다 처음 만난 사람처럼
같은 말을 반복한다. 게이트(쿨다운)와 맥락 제공자가 함께 읽는 공용 기억이라
따로 떼어냈다.

지금은 메모리에만 있어서 프로세스가 죽으면 날아간다. 저장소 레이어가 생기면
이 클래스의 구현만 갈아끼우면 된다 — 읽는 쪽 코드는 그대로다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass(frozen=True)
class SpeechRecord:
    trigger: str
    at: datetime
    text: str


@dataclass
class SpeechLog:
    _by_trigger: Dict[str, List[SpeechRecord]] = field(default_factory=dict)

    def record(self, trigger: str, at: datetime, text: str) -> None:
        self._by_trigger.setdefault(trigger, []).append(
            SpeechRecord(trigger=trigger, at=at, text=text)
        )

    def last(self, trigger: str) -> Optional[SpeechRecord]:
        entries = self._by_trigger.get(trigger)
        return entries[-1] if entries else None

    def since(self, moment: datetime) -> List[SpeechRecord]:
        """``moment`` 이후에 한 말 전부를 시간순으로."""
        found = [r for records in self._by_trigger.values() for r in records if r.at >= moment]
        return sorted(found, key=lambda r: r.at)
