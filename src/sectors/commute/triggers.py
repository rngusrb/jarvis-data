"""commute 섹터의 트리거."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean
from typing import List, Optional, Sequence

from src.core.models import Insight, Observation, Severity


@dataclass
class LateDepartureTrigger:
    """평소보다 늦게 퇴근했는지 본다.

    수면과 같은 원리다 — 절대 기준("7시 넘으면 야근")이 아니라 **본인 패턴 대비
    편차**로 본다. 6시 퇴근이 평범한 사람과 4시 퇴근이 평범한 사람에게 같은
    기준을 대면 한쪽은 매일 울리고 다른 쪽은 영영 안 울린다.
    """

    name: str = "late_departure"
    kind: str = "work_departure"
    # 하루 한 번뿐인 사건이라 창을 넓게 잡아야 기록 N개가 모인다.
    # 수면에서 창을 8일→21일로 넓힌 것과 같은 이유다.
    lookback: timedelta = timedelta(days=28)
    baseline_days: int = 10
    late_hours: float = 1.5
    very_late_hours: float = 3.0

    def _is_workday(self, observation: Observation) -> bool:
        """주말은 baseline 에서 뺀다.

        주말에 회사를 떠난 기록이 섞이면 평균이 흐트러진다. 주 5일 패턴을
        가진 사람의 baseline 은 평일로만 만들어야 한다.
        """
        return observation.at.weekday() < 5

    def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]:
        workdays: List[Observation] = [o for o in window if self._is_workday(o)]
        if len(workdays) < 4:
            # 패턴이라 부를 만큼 모이지 않았다. 며칠 치로 "평소"를 정하면
            # 그 평소가 우연이다.
            return None

        ordered = sorted(workdays, key=lambda o: o.at)
        latest = ordered[-1]
        baseline = ordered[-(self.baseline_days + 1) : -1]
        if not baseline:
            return None

        usual = mean(o.value for o in baseline)
        delta = latest.value - usual
        if delta < self.late_hours:
            return None

        severity = Severity.URGENT if delta >= self.very_late_hours else Severity.NOTABLE
        summary = (
            f"오늘 퇴근 {_clock(latest.value)}. 평소({_clock(usual)})보다 {delta:.1f}시간 늦음."
        )
        return Insight(
            trigger=self.name,
            summary=summary,
            severity=severity,
            at=latest.at,
            observations=tuple(ordered),
        )


def _clock(hour: float) -> str:
    """17.5 → '17:30'."""
    h, m = int(hour), round((hour - int(hour)) * 60)
    if m == 60:
        h, m = h + 1, 0
    return f"{h:02d}:{m:02d}"


TRIGGERS = [LateDepartureTrigger()]
