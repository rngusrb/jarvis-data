"""수면 관련 트리거."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from statistics import mean
from typing import List, Optional, Sequence

from src.core.models import Insight, Observation, Severity


@dataclass
class SleepDropTrigger:
    """평소 수면 대비 어젯밤이 유난히 짧았는지 본다.

    절대 기준("7시간 미만")이 아니라 **본인 baseline 대비 편차**를 쓴다.
    실제 데이터로 확인된 차이다 — 이 사용자의 4개월치에 7시간 기준을 대면
    83일 중 73일(88%)에 알림이 울린다. baseline 기준으로는 11회다.
    """

    name: str = "sleep_drop"
    kind: str = "sleep_hours"
    lookback: timedelta = timedelta(days=8)
    baseline_days: int = 7
    drop_hours: float = 1.5
    # 평소의 절반 이하로 잤으면 심각. 감소'량'이 아니라 '비율'인 이유는
    # 3시간 감소가 사람마다 다른 의미이기 때문이다 — 평소 8시간 자는 사람에겐
    # 37% 감소지만 평소 5시간 자는 사람에겐 61% 감소다.
    urgent_ratio: float = 0.5
    # 워치는 정상 수면을 Core/Deep/REM 단계로 쪼개 10~20조각을 남긴다.
    # 조각이 한두 개뿐이면 잠을 잔 게 아니라 측정이 실패한 것이다.
    min_segments: int = 3

    def _is_measured(self, observation: Observation) -> bool:
        """측정이 믿을 만한지 본다.

        값이 통계적으로 이상한지가 아니라 **측정 과정이 정상이었는지**를 본다.
        실제 데이터에서 0.18시간(조각 1개)은 통계로도 걸러지지만,
        2.82시간(조각 1개)은 값만 봐서는 정상으로 통과해버린다.

        단축어 등 다른 경로로 들어온 관측치엔 품질 정보가 없다.
        그때는 판단하지 않고 통과시킨다 — 모르는 것과 나쁜 것은 다르다.
        """
        segments = observation.meta.get("segments")
        if segments is None:
            return True
        try:
            return int(segments) >= self.min_segments
        except (TypeError, ValueError):
            return True

    def check(self, window: Sequence[Observation]) -> Optional[Insight]:
        # 측정 실패는 판단 대상에서도, baseline에서도 뺀다. baseline에 남겨두면
        # 평균을 끌어내려서 그 뒤 며칠의 판단까지 오염시킨다.
        measured: List[Observation] = [o for o in window if self._is_measured(o)]
        if len(measured) < 3:
            return None

        ordered = sorted(measured, key=lambda o: o.at)
        latest = ordered[-1]
        baseline = ordered[-(self.baseline_days + 1) : -1]
        if not baseline:
            return None

        avg = mean(o.value for o in baseline)
        delta = avg - latest.value
        if delta < self.drop_hours:
            return None

        severity = Severity.URGENT if latest.value <= avg * self.urgent_ratio else Severity.NOTABLE
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
