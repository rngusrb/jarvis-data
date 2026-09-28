"""수집이 멈춘 걸 감지하는 트리거.

자동 수집의 가장 흔한 실패는 요란한 에러가 아니라 **침묵**이다. 단축어가
안 돌면 아무 일도 일어나지 않는다. 자비스는 "볼 데이터가 없네" 하고 조용히
있고, 사용자는 "요즘 자비스가 말이 없네" 하고 넘어간다. 2주 뒤에야 알아챈다.

그래서 데이터가 안 들어오는 것 자체를 하나의 신호로 다룬다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from src.core.models import Insight, Observation, Severity


@dataclass
class StaleDataTrigger:
    # 기본값을 두지 않는다. 어느 지표를 감시할지는 부르는 쪽이 안다 —
    # 기본값이 있으면 플랫폼이 특정 지표를 전제하게 되고, 그 전제는
    # 지표를 접거나 이름을 바꿀 때 조용히 낡는다.
    kind: str
    label: str
    name: str = ""
    # 감시견에게는 창이 없다. 다른 트리거는 baseline을 계산하려고 최근
    # 구간만 보지만, 이쪽이 알아야 할 건 "마지막 기록이 언제냐" 하나다.
    #
    # 사고 이력: 2026-08-29 수집이 멈췄다. 자비스는 9월 12일까지 매일
    # 경고했고 그 뒤로 침묵했다. 창이 14일이라 8월 29일 기록이 창 밖으로
    # 밀려났고, 빈 창은 아래에서 "아직 시작 안 함"으로 판정됐다.
    # **중단이 심해질수록 감시견이 조용해졌다.** 한 달을 그렇게 보냈다.
    lookback: timedelta = timedelta(days=365 * 10)
    # 수면은 하루 한 번 들어온다. 36시간이면 하루를 통째로 건너뛴 것이라
    # 우연한 지연이 아니라 수집이 끊겼다고 봐야 한다.
    stale_after: timedelta = timedelta(hours=36)
    urgent_after: timedelta = timedelta(days=3)

    def __post_init__(self) -> None:
        # 종류별로 하나씩 두게 되므로 이름이 겹치면 쿨다운을 공유해버린다.
        if not self.name:
            self.name = f"stale_data:{self.kind}"

    def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]:
        if not window:
            # 한 번도 들어온 적이 없는 것은 "멈춤"이 아니라 "아직 시작 안 함"이다.
            # 설정을 마치기도 전에 잔소리를 듣게 할 이유가 없다.
            #
            # 이 판정이 맞으려면 창이 전체 기록을 덮어야 한다. 좁은 창에서는
            # "한 번도 없음"과 "오래전에 끊김"이 똑같이 빈 창으로 보인다.
            return None

        latest = max(window, key=lambda o: o.at)
        age = now - latest.at
        if age < self.stale_after:
            return None

        hours = age.total_seconds() / 3600
        severity = Severity.URGENT if age >= self.urgent_after else Severity.NOTABLE
        summary = (
            f"{self.label} 데이터가 {hours:.0f}시간째 들어오지 않음. "
            f"마지막 기록은 {latest.at:%m/%d}. 수집 자동화가 멈췄을 수 있음."
        )
        return Insight(
            trigger=self.name,
            summary=summary,
            severity=severity,
            at=now,
            observations=(latest,),
        )
