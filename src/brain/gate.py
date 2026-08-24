"""싼 검문소.

LLM을 부를 가치가 있는지만 판단한다. 숫자 비교뿐이라 공짜고, 결정적이라
"왜 자비스가 조용했는지"를 나중에 정확히 재현할 수 있다.

이 검문소가 에이전트보다 **앞에** 있는 게 핵심이다. 순서를 뒤집으면 30분마다
3090을 태워놓고 결국 "말할 필요 없음" 판정을 받는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.brain.memory import SpeechLog
from src.core.models import Insight, Severity


@dataclass
class Gate:
    log: SpeechLog = field(default_factory=SpeechLog)
    min_severity: Severity = Severity.NOTABLE
    cooldown: timedelta = timedelta(hours=6)

    def allows(self, insight: Insight, now: datetime) -> bool:
        if insight.severity < self.min_severity:
            return False
        last = self.log.last(insight.trigger)
        if last is not None and now - last.at < self.cooldown:
            return False
        return True
