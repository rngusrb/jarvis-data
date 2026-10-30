"""등록된 트리거는 실제로 들어오는 지표를 본다.

꺼진 지표(수집기 없음)를 보는 트리거는 영영 울리지 않는데, 울리지 않는 게 정상처럼
보인다. 사고: 2026-10-30 정리 때 late_departure 가 수집기 없는 work_departure 를 보며
30분마다 빈 창을 읽고 있는 걸 찾았다.
"""

from __future__ import annotations

from app.main import SECTORS


def test_등록된_트리거는_수집기가_있는_지표를_본다() -> None:
    cards = {metric.kind: metric for sector in SECTORS for metric in sector.METRICS}
    dead = [
        f"{trigger.name} → {trigger.kind}"
        for sector in SECTORS
        for trigger in sector.TRIGGERS
        if trigger.kind not in cards or not cards[trigger.kind].active
    ]
    assert dead == [], f"수집기 없는 지표를 보는 트리거: {dead}"
