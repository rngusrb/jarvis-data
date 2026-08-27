"""흔적 카드.

지표 카드(`metrics.py`)와 같은 역할을 텍스트 쪽에서 한다 — 어떤 종류의
흔적이 존재하는지, 누가 보내는지를 **한 곳에** 적어둔다. 카드가 없으면
수신구가 아무 `kind` 나 받아들이고, 오타 하나가 조용히 새 종류를 만든다.

카드가 지표보다 얇다. 접기(Fold)도, 충돌 정책도 없다. 흔적은 평균 내지
않고, 같은 흔적이 두 번 오면 그냥 무시하면 되기 때문이다.

대신 지표에 없는 게 하나 있다 — `sensitive`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, Iterable, List, Optional


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
