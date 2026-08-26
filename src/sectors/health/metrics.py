"""health 섹터가 소유한 지표 카드.

Apple Watch와 iPhone에서 오는 것들. 지표를 늘리려면 여기에 카드를 한 장
추가하면 되고, 수신구·배선·맥락 제공자는 손대지 않는다.
"""

from __future__ import annotations

from src.core.metrics import Conflict, Fold, Metric

SHORTCUT = "아이폰 단축어가 기상할 때 자동 전송"

METRICS = [
    Metric(
        kind="sleep_hours",
        label="수면",
        # 워치가 수면을 조각으로 남긴다. 합산 전에 겹침을 걷어내야 하고,
        # 조각 수 자체가 측정 품질 신호라 원본 구간이 서버까지 와야 한다.
        fold=Fold.SPANS,
        collector=SHORTCUT,
        # 단축어의 "최근 1일" 창이 어제보다 일찍 깬 날 어젯밤을 반토막 낸다.
        # 잘린 조각이 온전한 밤을 덮어쓰면 과거가 조용히 줄어든다.
        on_conflict=Conflict.KEEP_LARGER,
    ),
    Metric(
        kind="step_count",
        label="걸음수",
        fold=Fold.SUM,
        # 워치가 운동 중에는 걸음수를 초 단위로 쪼갠다 — 하루 표본이 3개에서
        # 1,112개까지 간다. 단축어 반복이 그 규모를 못 버티고 멈춘다.
        #
        # HealthKit 그룹화는 대안이 못 된다. 건강 앱이 쓰는 통계 질의와 달리
        # 소스를 걷어내지 않고 그냥 더해서, 8/18이 9,288 대신 15,480으로 온다.
        #
        # 걸음수는 이 프로덕트에서 중요한 신호가 아니라 여기서 멈춘다. 과거
        # 1,642일치는 남고, 필요해지면 백필이 언제든 채운다.
        collector=None,
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
