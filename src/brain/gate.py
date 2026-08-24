"""싼 검문소.

LLM을 부를 가치가 있는지만 판단한다. 숫자 비교뿐이라 공짜고, 결정적이라
"왜 자비스가 조용했는지"를 나중에 정확히 재현할 수 있다.

이 검문소가 에이전트보다 **앞에** 있는 게 핵심이다. 순서를 뒤집으면 30분마다
3090을 태워놓고 결국 "말할 필요 없음" 판정을 받는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict

from src.brain.memory import SpeechLog
from src.core.models import Insight, Severity


@dataclass
class Gate:
    log: SpeechLog = field(default_factory=SpeechLog)
    min_severity: Severity = Severity.NOTABLE
    cooldown: timedelta = timedelta(hours=6)
    # 트리거마다 적정 재발화 간격이 다르다. 수집 중단처럼 상태가 계속
    # 유지되는 신호는 기본 쿨다운으로 두면 하루 네 번씩 같은 말을 한다.
    cooldown_overrides: Dict[str, timedelta] = field(default_factory=dict)

    def cooldown_for(self, trigger: str) -> timedelta:
        return self.cooldown_overrides.get(trigger, self.cooldown)

    def allows(self, insight: Insight, now: datetime) -> bool:
        if insight.severity < self.min_severity:
            return False
        last = self.log.last(insight.trigger)
        if last is not None and now - last.at < self.cooldown_for(insight.trigger):
            return False
        return True
