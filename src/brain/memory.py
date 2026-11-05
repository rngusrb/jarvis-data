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

from dataclasses import dataclass, field, replace
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
    # 무엇에 대한 판단이었나 — 신호의 `at`(그 밤, 그 아침의 첫 사건). 게이트가 같은 것을 두 번
    # 묻지 않으려고 본다. 2026-11-02 전에 남은 기록에는 없다.
    about: Optional[datetime] = None
    # 먼저 건 말에 붙인 짧은 표. 👍👎 버튼이 이걸 들고 돌아온다. 말 안 건 판단에는 없다.
    token: Optional[str] = None
    # 사용자의 반응: 1(👍), -1(👎), 0(없음). 나중에 누른 게 이긴다.
    feedback: int = 0


class SpeechLog(Protocol):
    """발화 기억이 만족해야 할 전부. 읽는 쪽은 어느 구현인지 몰라도 된다."""

    def record(
        self,
        trigger: str,
        at: datetime,
        text: str,
        spoken: bool = True,
        about: Optional[datetime] = None,
        token: Optional[str] = None,
    ) -> None: ...

    def last(self, trigger: str) -> Optional[SpeechRecord]: ...

    def judged(self, trigger: str, about: datetime) -> bool:
        """이 트리거로 이 신호(`about`)를 판단한 적이 있나 — 마지막 기록만이 아니라 전부에서."""
        ...

    def last_spoken_in(self, family: str) -> Optional[SpeechRecord]:
        """줄기(`family:…` 트리거들) 안에서 마지막으로 **실제로 한 말**."""
        ...

    def since(self, moment: datetime) -> List[SpeechRecord]:
        """``moment`` 이후 **실제로 한 말**만. 맥락 제공자가 쓴다."""
        ...

    def rate(self, token: str, value: int) -> Optional[SpeechRecord]:
        """표가 가리키는 말에 반응을 남긴다. 그런 말이 없으면 None."""
        ...

    def rated(self, moment: datetime) -> List[SpeechRecord]:
        """``moment`` 이후에 한 말 중 반응(👍👎)이 붙은 것, 시간순."""
        ...


@dataclass
class InMemorySpeechLog:
    _by_trigger: Dict[str, List[SpeechRecord]] = field(default_factory=dict)

    def record(
        self,
        trigger: str,
        at: datetime,
        text: str,
        spoken: bool = True,
        about: Optional[datetime] = None,
        token: Optional[str] = None,
    ) -> None:
        """판단을 기록한다.

        사고 이력: 2026-09-29. 재시작 후 20시간 동안 LLM을 68번 부르고 2번
        말했다. 게이트가 쿨다운을 넘겨 통과시키면 LLM이 "말 안 걸겠다"를
        내놓는데, 그 판정을 아무 데도 안 남겨서 30분 뒤 똑같이 물어봤다.
        **"싼 게이트 먼저, 비싼 LLM 나중"이 여기서 새고 있었다.**
        """
        self._by_trigger.setdefault(trigger, []).append(
            SpeechRecord(trigger=trigger, at=at, text=text, spoken=spoken, about=about, token=token)
        )

    def last(self, trigger: str) -> Optional[SpeechRecord]:
        entries = self._by_trigger.get(trigger)
        return entries[-1] if entries else None

    def judged(self, trigger: str, about: datetime) -> bool:
        return any(r.about == about for r in self._by_trigger.get(trigger, []))

    def last_spoken_in(self, family: str) -> Optional[SpeechRecord]:
        found = [
            r
            for name, records in self._by_trigger.items()
            if name.startswith(f"{family}:")
            for r in records
            if r.spoken
        ]
        return max(found, key=lambda r: r.at) if found else None

    def rated(self, moment: datetime) -> List[SpeechRecord]:
        return [r for r in self.since(moment) if r.feedback]

    def rate(self, token: str, value: int) -> Optional[SpeechRecord]:
        for records in self._by_trigger.values():
            for i, r in enumerate(records):
                if r.token == token:
                    records[i] = replace(r, feedback=value)
                    return records[i]
        return None

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
