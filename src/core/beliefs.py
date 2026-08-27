"""믿음 — 자비스가 사용자에 대해 **추론해낸** 것.

관측치·흔적과 결정적으로 다르다. 앞의 둘은 일어난 사실이고, 믿음은 그것을
보고 자비스가 지어낸 해석이다. 틀릴 수 있다는 뜻이고, 그래서 셋이 필수다.

  근거   왜 그렇게 믿는지. 없으면 만들 수 없다.
  확신   얼마나 믿는지.
  수명   언제 시들지. 관심사는 원래 사라진다.

OpenClaw 같은 대화형 비서의 기억과 여기가 갈린다. 걔네 기억은 사용자가
"나 채식주의자야"라고 **말해준** 사실이라 확신도가 필요 없다. 우리 믿음은
검색 기록에서 **미루어 짐작한** 것이다.

`kind` 를 LLM이 만든다. 이 파일이 존재하는 진짜 이유가 그거다 — 사람이
미리 칸을 정해두면 "관심사:주거" 같은 건 예상해도 "요즘 밤에 라멘 레시피만
본다"는 예상 못 한다. 대신 자유롭게 두면 2주 만에 같은 걸 여섯 개 이름으로
저장하므로, 만들 때 기존 목록을 보여주는 규율이 반드시 함께 간다
(`brain/reflect.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Tuple


class Status(str, Enum):
    """믿음의 일생.

    근거가 한 번 나온 것과 여러 번 나온 것은 무게가 다르다. 한 번 검색해본
    것까지 "이 사람의 관심사"라고 프롬프트에 넣으면, 자비스는 스쳐간 호기심을
    붙들고 늘어지는 비서가 된다.
    """

    CANDIDATE = "candidate"  # 근거가 한 번. 아직 프롬프트에 안 넣는다.
    CONFIRMED = "confirmed"  # 근거가 또 나왔다. 이제 진짜다.
    FADING = "fading"  # 한동안 근거가 없다. 곧 지운다.


# 확정된 믿음이 이만큼 조용하면 시든다. 관심사는 해소되면 흔적이 끊긴다 —
# 이사를 가버리면 전세 검색이 멈추는 것과 같다.
FADE_AFTER = timedelta(days=14)
# 시든 뒤 이만큼 더 조용하면 지운다. 바로 안 지우는 건 잠깐 쉬었다 다시
# 관심이 붙는 경우가 흔하기 때문이다.
FORGET_AFTER = timedelta(days=30)


@dataclass(frozen=True)
class Belief:
    kind: str
    value: str
    confidence: float
    first_seen: datetime
    last_seen: datetime
    # 왜 그렇게 믿는지. **흔적의 텍스트를 그대로** 담는다.
    #
    # id 대신 텍스트인 이유: 사람이 "왜 자비스가 나를 이사 준비 중이라고
    # 생각했지"를 확인할 때 보고 싶은 건 id가 아니라 실제 검색어다. 흔적이
    # 나중에 정리돼도 근거는 남아야 한다.
    evidence: Tuple[str, ...] = ()
    status: Status = Status.CANDIDATE
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 근거 없는 믿음은 검증할 수 없다. 모델이 그럴듯한 문장을 지어내는 걸
        # 막는 유일한 장치라 저장소가 아니라 **모델 자체**에서 막는다.
        if not self.evidence:
            raise ValueError(f"근거 없는 믿음은 만들 수 없다: {self.kind}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"확신도는 0~1 사이여야 한다: {self.confidence}")

    def aged(self, now: datetime) -> Status:
        """지금 시점에서 이 믿음이 어느 단계인지.

        저장된 status를 그대로 믿지 않고 시간으로 다시 계산한다. 자비스가
        며칠 꺼져 있었어도 시드는 건 시들어야 하기 때문이다.
        """
        quiet = now - self.last_seen
        if self.status is Status.CANDIDATE:
            return Status.CANDIDATE
        if quiet >= FADE_AFTER:
            return Status.FADING
        return Status.CONFIRMED

    def forgettable(self, now: datetime) -> bool:
        return now - self.last_seen >= FORGET_AFTER
