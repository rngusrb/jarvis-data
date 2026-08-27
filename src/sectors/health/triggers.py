"""health 섹터의 트리거.

플랫폼(`src/triggers/`)에는 프로토콜과 도메인 무관한 것(수집 중단)만 남는다.
"수면이 평소보다 짧다"는 health 의 지식이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean
from typing import List, Optional, Sequence

from src.core.models import Insight, Observation, Severity

# 워치는 정상 수면을 Core/Deep/REM 단계로 쪼개 10~20조각을 남긴다.
# 조각이 한두 개뿐이면 잠을 잔 게 아니라 측정이 실패한 것이다.
MIN_SEGMENTS = 3


def is_measured(observation: Observation, min_segments: int = MIN_SEGMENTS) -> bool:
    """측정이 믿을 만한지 본다.

    값이 통계적으로 이상한지가 아니라 **측정 과정이 정상이었는지**를 본다.
    실제 데이터에서 0.18시간(조각 1개)은 통계로도 걸러지지만,
    2.82시간(조각 1개)은 값만 봐서는 정상으로 통과해버린다.

    단축어 등 다른 경로로 들어온 관측치엔 품질 정보가 없다.
    그때는 판단하지 않고 통과시킨다 — 모르는 것과 나쁜 것은 다르다.

    두 수면 트리거가 같은 기준을 써야 한다. 한쪽만 측정 실패를 걸러내면
    같은 밤을 두고 "쟤는 세고 얘는 안 세는" 상태가 되고, 왜 그런지는
    값이 아니라 코드 위치에 있어서 추적이 지독히 어려워진다.
    """
    segments = observation.meta.get("segments")
    if segments is None:
        return True
    try:
        return int(segments) >= min_segments
    except (TypeError, ValueError):
        return True


@dataclass
class SleepDropTrigger:
    """평소 수면 대비 어젯밤이 유난히 짧았는지 본다.

    절대 기준("7시간 미만")이 아니라 **본인 baseline 대비 편차**를 쓴다.
    실제 데이터로 확인된 차이다 — 이 사용자의 4개월치에 7시간 기준을 대면
    83일 중 73일(88%)에 알림이 울린다. baseline 기준으로는 11회다.
    """

    name: str = "sleep_drop"
    kind: str = "sleep_hours"
    # 창이 8일이면 "매일 기록이 있다"를 전제하게 된다. 실제로는 워치를 안 찬
    # 날이 섞여 커버리지가 70% 남짓이고 최장 6일까지 비는데, 그러면 창 안에
    # 기록이 3건도 안 모여 판단 자체를 포기하는 밤이 생긴다.
    # baseline은 "최근 7일"이 아니라 "최근 기록 7개"다. 창은 그 7개를
    # 찾을 만큼만 넓으면 된다.
    lookback: timedelta = timedelta(days=21)
    baseline_days: int = 7
    drop_hours: float = 1.5
    # 평소의 절반 이하로 잤으면 심각. 감소'량'이 아니라 '비율'인 이유는
    # 3시간 감소가 사람마다 다른 의미이기 때문이다 — 평소 8시간 자는 사람에겐
    # 37% 감소지만 평소 5시간 자는 사람에겐 61% 감소다.
    urgent_ratio: float = 0.5
    min_segments: int = MIN_SEGMENTS

    def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]:
        # 측정 실패는 판단 대상에서도, baseline에서도 뺀다. baseline에 남겨두면
        # 평균을 끌어내려서 그 뒤 며칠의 판단까지 오염시킨다.
        measured: List[Observation] = [o for o in window if is_measured(o, self.min_segments)]
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


@dataclass
class ChronicShortSleepTrigger:
    """짧은 수면이 예외가 아니라 평소가 됐을 때 본다.

    `SleepDropTrigger` 의 사각지대를 메운다. 저쪽은 baseline 대비 급락을
    보는데, baseline 자체가 내려앉으면 아무것도 안 잡힌다.

    실제 사고: 2026-08-27 이 사용자는 2.77시간을 잤다. 그런데 최근 7개
    평균이 3.91시간이라 차이가 1.14시간, 문턱(1.5)에 못 미쳐 자비스는
    침묵했다. 평소가 망가져 있어서 "평소랑 비슷하다"는 판정이 나온 것이다.

    그래서 여기서만 **절대 기준**을 쓴다 — CLAUDE.md 의 원칙("절대 기준이
    아니라 개인 baseline 대비 편차")에 대한 의도적 예외다. 대신 하루가 아니라
    **추세**에 건다. 하루에 걸면 만성인 사람에게 매일 울려서 알림 스팸이
    되는데, 그게 정확히 저 원칙이 막으려던 실패다. 평균에 걸면 상태가
    실제로 바뀔 때까지 조용하다.

    급변은 여전히 `SleepDropTrigger` 가 본다. 둘은 서로 다른 것을 말한다 —
    "어젯밤이 유난했다" 와 "요즘이 계속 이렇다".
    """

    name: str = "chronic_short_sleep"
    kind: str = "sleep_hours"
    lookback: timedelta = timedelta(days=21)
    # baseline 과 같은 이유로 "최근 며칠"이 아니라 "최근 기록 몇 개"다.
    # 워치를 안 찬 날이 섞여 커버리지가 70% 남짓이다.
    sample_size: int = 7
    # 성인 권장 수면의 하한. 하루치가 아니라 **평균이** 이 아래로 내려가면
    # 그건 어젯밤의 사정이 아니라 생활 패턴이다.
    healthy_hours: float = 6.0
    # 이 아래면 평균 자체가 위태롭다. 4시간은 권장치의 3분의 2다.
    urgent_hours: float = 4.0
    # 기록 서너 개로 "요즘 계속"이라고 말할 수는 없다.
    min_samples: int = 5
    min_segments: int = MIN_SEGMENTS

    def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]:
        measured = [o for o in window if is_measured(o, self.min_segments)]
        if len(measured) < self.min_samples:
            return None

        recent = sorted(measured, key=lambda o: o.at)[-self.sample_size :]
        avg = mean(o.value for o in recent)
        if avg >= self.healthy_hours:
            return None

        severity = Severity.URGENT if avg < self.urgent_hours else Severity.NOTABLE
        span_days = (recent[-1].at - recent[0].at).days + 1
        summary = (
            f"최근 {span_days}일간 기록 {len(recent)}개의 평균 수면이 {avg:.1f}시간. "
            f"권장 하한({self.healthy_hours:.0f}시간)을 계속 밑돌고 있음. "
            f"어제 하루의 문제가 아니라 요즘 내내 이렇다."
        )
        return Insight(
            trigger=self.name,
            summary=summary,
            severity=severity,
            at=recent[-1].at,
            observations=tuple(recent),
        )


# 섹터가 자기 트리거를 들고 나간다. app/main.py 는 이 목록을 그대로 받는다.
TRIGGERS = [SleepDropTrigger(), ChronicShortSleepTrigger()]
