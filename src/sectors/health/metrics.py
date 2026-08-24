"""health 섹터가 소유한 지표 카드.

Apple Watch와 iPhone에서 오는 것들. 지표를 늘리려면 여기에 카드를 한 장
추가하면 되고, 수신구·배선·맥락 제공자는 손대지 않는다.
"""

from __future__ import annotations

from src.core.metrics import Fold, Metric

SHORTCUT = "아이폰 단축어가 기상할 때 자동 전송"

METRICS = [
    Metric(
        kind="sleep_hours",
        label="수면",
        # 워치가 수면을 조각으로 남긴다. 합산 전에 겹침을 걷어내야 하고,
        # 조각 수 자체가 측정 품질 신호라 원본 구간이 서버까지 와야 한다.
        fold=Fold.SPANS,
        collector=SHORTCUT,
    ),
    Metric(
        kind="step_count",
        label="걸음수",
        fold=Fold.SUM,
        collector=SHORTCUT,
    ),
    Metric(
        kind="resting_heart_rate",
        label="휴식기 심박",
        fold=Fold.MEAN,
        collector=SHORTCUT,
    ),
    Metric(
        kind="heart_rate_avg",
        label="심박(원본)",
        fold=Fold.MEAN,
        # 원본 심박은 하루 361개씩 쌓여 단축어가 반복을 끝내지 못했고, 운동 중
        # 심박까지 섞여 신호로도 둔했다. 워치가 이미 계산해두는 휴식기 심박으로
        # 갈아탔다. 과거 데이터는 남기되 더 이상 수집하지 않는다.
        collector=None,
    ),
]
