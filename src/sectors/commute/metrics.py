"""commute 섹터가 소유한 지표 카드.

위치 자동화가 주는 것은 "언제 어디를 떠났다/도착했다"는 **시각 하나**다.
그 시각을 하루 단위 관측치로 삼는다 — 값이 곧 시각(시 단위 실수)이다.

    17.5  →  오후 5시 30분

수면·심박과 달리 측정 표본이 아니라 **사건**이라, 하루에 여러 번 일어나도
의미 있는 건 하나다. 퇴근은 마지막, 출근 도착은 처음.
"""

from __future__ import annotations

from datetime import timedelta

from src.core.metrics import Fold, Metric

AUTOMATION = "아이폰 위치 자동화 (회사 도착/떠남)"

# 하루 한 번뿐인 사건이라 하루를 건너뛰는 건 흔하다 (주말·휴가·재택).
# 수면(36시간)과 같은 기준을 쓰면 매주 월요일마다 잔소리를 듣는다.
STALE_AFTER = timedelta(days=4)

METRICS = [
    Metric(
        kind="work_departure",
        label="퇴근 시각",
        # 하루에 여러 번 나갔다 들어와도 "퇴근"은 마지막 한 번이다.
        # 평균을 내면 3시와 7시 사이 어딘가라는, 실제로 없었던 시각이 나온다.
        fold=Fold.LAST,
        collector=AUTOMATION,
        stale_after=STALE_AFTER,
    ),
    Metric(
        kind="work_arrival",
        label="출근 도착 시각",
        fold=Fold.FIRST,
        collector=AUTOMATION,
        stale_after=STALE_AFTER,
    ),
]
