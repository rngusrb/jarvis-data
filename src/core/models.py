"""자비스 전 레이어가 공유하는 도메인 모델.

수집(parsers/pipelines) → 감지(triggers) → 판단(brain) → 발신(channels)까지
오직 이 파일의 타입만 주고받는다. 레이어끼리 서로의 내부 구현을 모르게 하려는 것 —
나중에 채널을 텔레그램에서 iOS 푸시로 바꿔도 위쪽 레이어는 손댈 일이 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any, Dict, Protocol, Sequence, Tuple


class Severity(IntEnum):
    """말을 걸 만한 정도.

    IntEnum이라 ``insight.severity >= Severity.NOTABLE`` 같은 임계값 비교가
    그대로 된다. 값 사이를 띄워둔 건 나중에 중간 단계를 끼워넣기 위해서.
    """

    INFO = 10
    NOTABLE = 20
    URGENT = 30


@dataclass(frozen=True)
class Observation:
    """정규화가 끝난 관측치 하나.

    파서가 무엇을 읽었든(Health XML, chat.db, .ics) 결국 이 모양으로 떨어진다.
    frozen인 건 파이프라인을 흘러가는 동안 누가 값을 바꾸지 못하게 하려는 것.
    """

    source: str
    kind: str
    value: float
    at: datetime
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Insight:
    """자비스가 사용자에게 말할 수 있는 "후보".

    후보일 뿐 아직 발송 대상이 아니다. 실제로 입을 열지는 brain.Judge가 정한다.
    """

    trigger: str
    summary: str
    severity: Severity
    at: datetime
    observations: Tuple[Observation, ...] = ()


class ObservationCatalog(Protocol):
    """무엇이 언제까지 들어와 있는지 아는 저장소.

    ObservationSource와 나눠둔 이유는 둘의 쓰임이 다르기 때문이다. 트리거는
    관측치를 읽고(Source), 맥락 제공자는 수집 상태를 읽는다(Catalog).
    """

    def last_seen(self) -> Dict[str, datetime]:
        """종류별 최신 관측 시각."""
        ...


class ObservationSource(Protocol):
    """저장소가 만족해야 할 최소 계약.

    자비스 루프는 Qdrant인지 SQLite인지 알 필요가 없다. 이것만 있으면 돈다.
    """

    def recent(self, kind: str, since: datetime) -> Sequence[Observation]:
        """``since`` 이후의 ``kind`` 관측치를 시간순으로 돌려준다."""
        ...
