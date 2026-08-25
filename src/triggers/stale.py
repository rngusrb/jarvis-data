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
    lookback: timedelta = timedelta(days=14)
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
