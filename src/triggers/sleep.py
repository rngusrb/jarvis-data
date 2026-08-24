"""수면 관련 트리거."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from statistics import mean
from typing import Optional, Sequence

from src.core.models import Insight, Observation, Severity


@dataclass
class SleepDropTrigger:
    """평소 수면 대비 어젯밤이 유난히 짧았는지 본다.

    절대 기준("7시간 미만")이 아니라 **본인 baseline 대비 편차**를 쓰는 게 핵심이다.
    초개인화 에이전트에서 절대 기준은 거의 항상 틀린다 — 평소 6시간 자는 사람에게
    "7시간 못 잤어요"는 매일 울리는 알람이고, 그 순간 사용자는 알림을 끈다.
    """

    name: str = "sleep_drop"
    kind: str = "sleep_hours"
    lookback: timedelta = timedelta(days=8)
    baseline_days: int = 7
    drop_hours: float = 1.5
    urgent_drop_hours: float = 3.0

    def check(self, window: Sequence[Observation]) -> Optional[Insight]:
        if len(window) < 3:
            # baseline을 만들 데이터가 없으면 판단 자체를 하지 않는다.
            return None

        ordered = sorted(window, key=lambda o: o.at)
        latest = ordered[-1]
        baseline = ordered[-(self.baseline_days + 1) : -1]
        if not baseline:
            return None

        avg = mean(o.value for o in baseline)
        delta = avg - latest.value
        if delta < self.drop_hours:
            return None

        severity = Severity.URGENT if delta >= self.urgent_drop_hours else Severity.NOTABLE
        summary = (
            f"어젯밤 수면 {latest.value:.1f}시간. "
            f"최근 {len(baseline)}일 평균({avg:.1f}시간)보다 {delta:.1f}시간 짧음."
        )
        return Insight(
            trigger=self.name,
            summary=summary,
            severity=severity,
            at=latest.at,
            observations=tuple(ordered),
        )
