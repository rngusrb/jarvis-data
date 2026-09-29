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
    """자비스가 이 트리거를 **판단한** 기록. 말했든 안 했든 남는다.

    `spoken` 이 갈라놓는 게 이 클래스의 핵심이다. 읽는 쪽이 둘인데 원하는
    것이 다르다.

      게이트   "언제 마지막으로 판단했나"  → spoken 무관
      맥락     "무슨 말을 했나"            → spoken=True 만

    합쳐두면 LLM이 "말 안 걸겠다"고 한 판정이 아무 데도 안 남아서, 게이트가
    같은 신호를 30분마다 다시 통과시킨다 (아래 record 의 사고 이력).
    """

    trigger: str
    at: datetime
    text: str
    spoken: bool = True


class SpeechLog(Protocol):
    """발화 기억이 만족해야 할 전부. 읽는 쪽은 어느 구현인지 몰라도 된다."""

    def record(self, trigger: str, at: datetime, text: str, spoken: bool = True) -> None: ...

    def last(self, trigger: str) -> Optional[SpeechRecord]: ...

    def since(self, moment: datetime) -> List[SpeechRecord]: ...

    """``moment`` 이후 **실제로 한 말**만. 맥락 제공자가 쓴다."""


@dataclass
class InMemorySpeechLog:
    _by_trigger: Dict[str, List[SpeechRecord]] = field(default_factory=dict)

    def record(self, trigger: str, at: datetime, text: str, spoken: bool = True) -> None:
        """판단을 기록한다.

        사고 이력: 2026-09-29. 재시작 후 20시간 동안 LLM을 68번 부르고 2번
        말했다. 게이트가 쿨다운을 넘겨 통과시키면 LLM이 "말 안 걸겠다"를
        내놓는데, 그 판정을 아무 데도 안 남겨서 30분 뒤 똑같이 물어봤다.
        **"싼 게이트 먼저, 비싼 LLM 나중"이 여기서 새고 있었다.**
        """
        self._by_trigger.setdefault(trigger, []).append(
            SpeechRecord(trigger=trigger, at=at, text=text, spoken=spoken)
        )

    def last(self, trigger: str) -> Optional[SpeechRecord]:
        entries = self._by_trigger.get(trigger)
        return entries[-1] if entries else None

    def since(self, moment: datetime) -> List[SpeechRecord]:
        """``moment`` 이후에 **실제로 한 말**을 시간순으로.

        말 안 걸기로 한 판정은 빼야 한다. 프롬프트에 섞이면 자비스가 하지도
        않은 말을 했다고 착각한다.
        """
        found = [
            r
            for records in self._by_trigger.values()
            for r in records
            if r.at >= moment and r.spoken
        ]
        return sorted(found, key=lambda r: r.at)
