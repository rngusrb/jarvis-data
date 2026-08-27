"""자비스가 "언제 무슨 말을 했는지" 기억하는 곳.

사람으로 치면 대화 기억이다. 이게 없으면 자비스는 30분마다 처음 만난 사람처럼
같은 말을 반복한다. 게이트(쿨다운)와 맥락 제공자가 함께 읽는 공용 기억이라
따로 떼어냈다.

구현이 둘이다.

  - `InMemorySpeechLog` : 테스트와 일회성 실행용. 프로세스가 죽으면 날아간다.
  - `SQLiteSpeechLog`   : 실제 운영용 (src/storage/speech.py).

쿨다운이 몇 시간짜리일 때는 메모리로도 버텼다. 주 단위 쿨다운이 생기면서
버틸 수 없게 됐다 — 재시작 한 번에 "요즘 잠이 부족하네요"를 다시 하게 된다.
서버는 배포할 때마다 재시작되므로 그건 곧 알림 스팸이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Protocol


@dataclass(frozen=True)
class SpeechRecord:
    trigger: str
    at: datetime
    text: str


class SpeechLog(Protocol):
    """발화 기억이 만족해야 할 전부. 읽는 쪽은 어느 구현인지 몰라도 된다."""

    def record(self, trigger: str, at: datetime, text: str) -> None: ...

    def last(self, trigger: str) -> Optional[SpeechRecord]: ...

    def since(self, moment: datetime) -> List[SpeechRecord]: ...


@dataclass
class InMemorySpeechLog:
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
