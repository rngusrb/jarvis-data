"""흔적 카드.

지표 카드(`metrics.py`)와 같은 역할을 텍스트 쪽에서 한다 — 어떤 종류의
흔적이 존재하는지, 누가 보내는지를 **한 곳에** 적어둔다. 카드가 없으면
수신구가 아무 `kind` 나 받아들이고, 오타 하나가 조용히 새 종류를 만든다.

카드가 지표보다 얇다. 접기(Fold)도, 충돌 정책도 없다. 흔적은 평균 내지
않고, 같은 흔적이 두 번 오면 그냥 무시하면 되기 때문이다.

대신 지표에 없는 게 셋 있다 — `sensitive`, `reflect`, `digest`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from src.core.models import Trace

# 창(since~until) 안의 흔적을 하루 한 줄로. 창 앞에서 잘린 날은 빼고(덜 쓴 날로 읽힌다),
# until 이 든 날은 아직 끝나지 않은 날로 적는다. 하루가 언제 바뀌는지는 섹터가 정한다.
Digest = Callable[[Sequence[Trace], datetime, datetime], List[str]]


@dataclass(frozen=True)
class TraceKind:
    kind: str
    label: str
    # 누가 보내나. None이면 접힌(retired) 종류 — 지표 카드와 같은 규칙이다.
    collector: Optional[str] = None
    # 수집이 끊긴 걸 알아채는 기준. 브라우저 기록은 하루에도 수십 건이지만
    # 파일 목록은 며칠씩 조용할 수 있어 종류마다 다르다.
    stale_after: timedelta = timedelta(days=3)
    # 저장은 하되 **판단 재료로는 기본에서 빼는** 종류.
    #
    # 검색 기록에는 본인도 자비스가 언급 안 했으면 하는 게 섞인다. 수집을
    # 좁히는 대신(안 모은 데이터는 나중에 소급이 안 된다) 프롬프트에 넣을지를
    # 따로 정한다. 이건 프라이버시 장치이면서 품질 장치이기도 하다 —
    # 흔적 300건을 매 주기 프롬프트에 밀어넣으면 맥락이 잡음으로 찬다.
    sensitive: bool = False
    # 어느 기기를 거쳐 오나. 지표 카드의 via 와 같은 뜻이다.
    via: Optional[str] = None
    # 회고가 낱줄로 훑는가. 회고는 종류별 최근 줄을 프롬프트에 그대로 넣는데, 앱 사용
    # 구간처럼 하루 수백 줄인 흔적은 관심사가 아니라 잡음이 되고 토큰만 먹는다. 그런 건
    # 아래 하루 요약으로 넣는다.
    reflect: bool = True
    # 낱줄 대신 보여줄 하루 요약. 회고는 낱줄로 안 훑는 종류(reflect=False)에 이걸 넣고,
    # 자비스의 맥락(반응·대화)은 이게 있는 종류를 어제·오늘치로 본다.
    #
    # **저장하지 않는다.** 볼 때마다 흔적에서 다시 접는다 — 흔적은 쌓이고 요약은 보는 방법이다.
    # 섹터가 만든다. 플랫폼은 흔적의 메타 키(앱 이름, 쓴 시간)를 모른다.
    digest: Optional[Digest] = None

    @property
    def active(self) -> bool:
        return self.collector is not None


class TraceRegistry:
    """섹터들이 자기 흔적 종류를 등록하는 곳."""

    def __init__(self) -> None:
        self._cards: Dict[str, TraceKind] = {}

    def register(self, kinds: Iterable[TraceKind]) -> "TraceRegistry":
        for card in kinds:
            if card.kind in self._cards:
                # 두 섹터가 같은 kind를 주장하면 저장소에서 섞인다. 조용히
                # 덮어쓰면 어느 쪽이 이겼는지 알 수 없으므로 여기서 멈춘다.
                raise ValueError(f"흔적 종류 '{card.kind}'가 이미 등록되어 있다")
            self._cards[card.kind] = card
        return self

    def get(self, kind: str) -> Optional[TraceKind]:
        return self._cards.get(kind)

    def all(self) -> List[TraceKind]:
        return sorted(self._cards.values(), key=lambda c: c.kind)

    def active(self) -> List[TraceKind]:
        return [c for c in self.all() if c.active]
