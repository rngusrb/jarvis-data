"""commute 섹터가 소유한 지표 카드.

이동은 두 종류의 관측치를 만든다.

  위치      "언제 어디에 있었나" — 점을 그대로 쌓는다. 패턴을 찾으려면
            하루로 접으면 안 된다. "하루 평균 위치"는 아무 의미가 없다.
  퇴근 시각  사건의 시각 자체가 값. 하루에 여러 번이면 마지막이 퇴근.

지금은 **위치를 쌓는 단계**다. 어디로 가는지 패턴이 보이기 전에는
트리거를 만들지 않는다 — 뭘 감지할지 모르는 채로 감지기를 짜는 꼴이다.
"""

from __future__ import annotations

from datetime import timedelta

from src.core.metrics import Fold, Metric

LEAVE = "아이폰 '떠날 때' 자동화 (집·직장 등을 벗어나는 순간)"
ARRIVE = "아이폰 '도착' 자동화"

# 하루 한 번뿐인 사건이라 주말·휴가·재택에 자연히 빈다.
# 수면과 같은 36시간을 쓰면 매주 월요일마다 잔소리를 듣는다.
STALE_AFTER = timedelta(days=4)

METRICS = [
    Metric(
        kind="location",
        label="위치",
        # 접지 않는다. value 는 GPS 정확도(m)이고 좌표는 meta 에 실린다 —
        # 500m 오차로 잡힌 점은 패턴 분석에서 빼야 하므로 품질 신호가 필요하다.
        fold=Fold.RAW,
        collector=LEAVE,
        stale_after=STALE_AFTER,
    ),
    Metric(
        kind="work_departure",
        label="퇴근 시각",
        # 하루에 여러 번 나갔다 들어와도 "퇴근"은 마지막 한 번이다.
        # 평균을 내면 3시와 7시 사이 어딘가라는, 실제로 없었던 시각이 나온다.
        fold=Fold.LAST,
        collector=None,  # 위치 패턴이 보이면 그때 켠다
        stale_after=STALE_AFTER,
    ),
    Metric(
        kind="work_arrival",
        label="출근 도착 시각",
        fold=Fold.FIRST,
        collector=None,
        stale_after=STALE_AFTER,
    ),
]
