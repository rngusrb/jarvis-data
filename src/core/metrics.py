"""지표 카드 — 한 지표에 대해 알아야 할 것을 한 장에 모은다.

이 파일이 생기기 전에는 `sleep_hours` 하나를 네 파일이 조금씩 알고 있었다.
파서는 이름을, 수신구는 접는 법을, 배선은 수집 경로와 라벨을, 트리거는 끊김
기준을. 하나를 고치고 나머지를 잊으면 조용히 어긋났다 — 휴식기 심박으로
갈아탈 때 실제로 그랬다.

**카드는 섹터가 소유하고, 플랫폼은 카드를 읽기만 한다.** 그래서 수신구는
"sleep_hours"라는 단어를 몰라도 되고, 새 섹터는 자기 카드를 들고 들어온다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Dict, Iterable, List, Optional


class Fold(str, Enum):
    """원본을 하루치로 줄이는 방식.

    **어떻게** 접는지는 플랫폼이 구현하고, **어느 방식**인지는 카드가 고른다.
    이 선택이 백필과 수신구 양쪽에 흩어져 있으면 경로에 따라 값이 달라진다.
    """

    SPANS = "spans"  # 구간들 → 밤 단위 (수면)
    SUM = "sum"  # 점들 → 하루 합계 (걸음수)
    MEAN = "mean"  # 점들 → 하루 평균 (심박)
    # 사건의 시각 자체가 값인 지표를 위한 것. 하루에 여러 번 일어나도
    # 의미가 있는 건 하나다 — 퇴근은 마지막, 출근 도착은 처음.
    FIRST = "first"
    LAST = "last"
    # 접지 않는다. 하루에 여러 점이 있어야 의미가 생기는 지표용 — 위치처럼
    # "언제 어디였나"가 낱낱이 남아야 패턴을 찾을 수 있다.
    RAW = "raw"


class Conflict(str, Enum):
    """같은 (출처, 종류, 시각)이 다시 들어왔을 때 무엇을 남길지.

    수집 창이 밀리면서 **같은 밤이 잘린 채 다시 오는** 경로가 있다. 단축어의
    "최근 1일"은 24시간 롤링이라, 어제보다 일찍 깬 날엔 창의 시작 경계가
    어젯밤 한가운데를 지나간다. 그러면 8시간 잔 밤이 30분으로 덮어써진다.
    """

    REPLACE = "replace"  # 나중 것이 이긴다. 기본값
    # 부분 창이 완전한 기록을 못 덮게. 창이 잘리면 값은 **짧아질 수만** 있고
    # (겹침 병합이 부풀리는 걸 막는다) 길어질 수 없으므로, 긴 쪽이 더 완전하다.
    KEEP_LARGER = "keep_larger"


@dataclass(frozen=True)
class Metric:
    kind: str
    label: str
    fold: Fold
    # 어떻게 수집되는지. None이면 **일부러 접은** 지표다 — 과거 데이터는 남기되
    # 되살릴 필요가 없다는 뜻. 레지스트리에 아예 없는 것(= 모르는 지표)과 다르다.
    collector: Optional[str] = None
    stale_after: timedelta = timedelta(hours=36)
    on_conflict: Conflict = Conflict.REPLACE

    @property
    def active(self) -> bool:
        return self.collector is not None


class MetricRegistry:
    """섹터들이 자기 카드를 등록하는 곳."""

    def __init__(self) -> None:
        self._cards: Dict[str, Metric] = {}

    def register(self, metrics: Iterable[Metric]) -> "MetricRegistry":
        for metric in metrics:
            if metric.kind in self._cards:
                # 두 섹터가 같은 kind를 주장하면 저장소에서 섞인다. 조용히
                # 덮어쓰면 어느 쪽이 이겼는지 알 수 없으므로 여기서 멈춘다.
                raise ValueError(f"지표 '{metric.kind}'가 이미 등록되어 있다")
            self._cards[metric.kind] = metric
        return self

    def get(self, kind: str) -> Optional[Metric]:
        return self._cards.get(kind)

    def all(self) -> List[Metric]:
        return sorted(self._cards.values(), key=lambda m: m.kind)

    def active(self) -> List[Metric]:
        return [m for m in self.all() if m.active]
