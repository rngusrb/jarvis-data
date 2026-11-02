"""health 섹터의 트리거.

플랫폼(`src/triggers/`)에는 프로토콜과 도메인 무관한 것(수집 중단)만 남는다.
"수면이 평소보다 짧다"는 health 의 지식이다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, median
from typing import List, Optional, Sequence, Set

from src.core.clock import local
from src.core.models import Insight, Observation, Severity

logger = logging.getLogger(__name__)

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
def _after_noon(at: datetime) -> float:
    """잠든 시각을 정오부터 잰 시간으로. 23:00 → 11.0, 01:30 → 13.5, 17:00 → 5.0.

    정오는 수면 집계가 밤을 나누는 경계다(`nights_from_spans`). 같은 경계로 재야 한 밤 안에서
    시각이 단조롭다. 저녁 6시로 쟀을 땐 17:00 에 시작한 잠(다음 밤에 배정된다)을 "15시간 늦게
    잠듦"으로 읽었다(2026-11-02 JAR-017 리뷰 재현).
    """
    when = local(at)
    return (when.hour + when.minute / 60 - 12) % 24


def _clock_after_noon(hours: float) -> str:
    total = round((hours + 12) * 60) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _onset(observation: Observation) -> Optional[datetime]:
    """잠든 시각. 없거나 읽히지 않으면 None — 그 밤은 판단에서 뺀다.

    여기서 예외가 나면 루프의 다른 트리거와 수집 감시까지 그 주기를 건너뛴다(리뷰 재현).
    """
    raw = observation.meta.get("onset")
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        logger.warning("잠든 시각을 읽지 못했다 — 그 밤은 뺀다: %r", raw)
        return None
    return when if when.tzinfo else None


@dataclass
class LateSleepTrigger:
    """평소보다 유난히 늦게 잠들었는지 — 리듬이 무너진 신호.

    수면 시간과는 다른 신호다. 일곱 시간을 자도 새벽 네 시에 잠들었다면 리듬이 밀린 것이다.
    기준은 본인의 최근 기록 일곱 개의 **중앙값**이다(절대 기준 "자정 넘으면 늦음"을 대면 새벽
    두 시가 평소인 사람에게 매일 울린다 — 이 사용자의 export 82밤 중 잠든 시각이 1~3시인 밤이
    72밤이다). 잠든 시각은 수면 집계가 남기는 `meta.onset` 이다(JAR-013).

    **오늘 아침에 끝난 밤만** 판단한다. 새 밤이 안 들어오면 지난 밤을 며칠씩 "어젯밤"으로
    다시 말했다(리뷰 재현: 워치 없는 밤이 이어지면 사흘 동안 같은 말).

    백테스트 (export 82밤, 정오 기준·중앙값): 문턱 1.0h → 15회, 1.5h → 8회(10밤에 한 번),
    2.0h → 5회. 1.5h 로 정했다. 같은 밤 sleep_drop 과 겹친 경우 2회.
    """

    name: str = "late_sleep"
    kind: str = "sleep_hours"
    lookback: timedelta = timedelta(days=21)
    baseline_days: int = 7
    late_hours: float = 1.5
    min_segments: int = MIN_SEGMENTS
    # 넘긴 밤을 한 번만 남기려는 기억. 판단에는 쓰지 않는다.
    _skipped: Set[datetime] = field(default_factory=set, repr=False, compare=False)

    def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]:
        nights = sorted(
            (o for o in window if is_measured(o, self.min_segments) and _onset(o) is not None),
            key=lambda o: o.at,
        )
        if len(nights) < 4:
            return None
        latest = nights[-1]
        baseline = nights[-(self.baseline_days + 1) : -1]
        usual = median(_after_noon(_onset(o) or latest.at) for o in baseline)
        slept = _after_noon(_onset(latest) or latest.at)
        if slept - usual < self.late_hours:
            return None
        if local(latest.at).date() != local(now).date():
            # 늦은 밤이지만 오늘 아침 끝난 밤이 아니다 — 자정을 넘겨 들어왔거나 새 밤이 없다.
            # 말하지 않되 한 번은 남긴다. 안 남기면 "이탈 없음"과 구별되지 않는다(재리뷰).
            if latest.at not in self._skipped:
                self._skipped.add(latest.at)
                logger.info("늦게 잠든 밤이지만 오늘 밤이 아니라 넘긴다: %s", latest.at.date())
            return None
        return Insight(
            trigger=self.name,
            summary=(
                f"어젯밤 {_clock_after_noon(slept)}에 잠들었다. 최근 {len(baseline)}일 평소"
                f"({_clock_after_noon(usual)})보다 {slept - usual:.1f}시간 늦음."
            ),
            severity=Severity.NOTABLE,
            at=latest.at,
            observations=tuple(nights),
        )


TRIGGERS = [SleepDropTrigger(), ChronicShortSleepTrigger(), LateSleepTrigger()]
