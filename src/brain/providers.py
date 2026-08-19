"""실제 맥락 제공자들.

지금은 이미 손에 있는 정보(관측치 추이, 최근 발화)만 쓴다. 캘린더 파서와
저장소가 생기면 ScheduleProvider, ProfileProvider 같은 게 여기 늘어난다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from src.brain.context import ContextBlock
from src.brain.memory import SpeechLog
from src.core.models import Insight


@dataclass
class ObservationTrendProvider:
    """신호에 딸려온 관측치를 추이로 펼쳐준다.

    요약문("평균보다 2시간 짧음")만 주면 모델이 판단할 근거가 얇다.
    실제 숫자 흐름을 보여주면 "3일째 계속 줄고 있네" 같은 말을 할 수 있게 된다.
    """

    name: str = "observation_trend"
    max_points: int = 7

    def fetch(self, insight: Insight, now: datetime) -> Optional[ContextBlock]:
        if len(insight.observations) < 2:
            return None
        recent = sorted(insight.observations, key=lambda o: o.at)[-self.max_points :]
        kind = recent[0].kind
        lines = [f"- {o.at:%m/%d} {o.value:g}" for o in recent]
        return ContextBlock(label=f"{kind} 최근 추이", body="\n".join(lines))


@dataclass
class SpeechHistoryProvider:
    """최근에 자비스가 뭐라고 했는지 알려준다.

    게이트(쿨다운)가 같은 트리거의 재발화를 막는다면, 이건 *다른* 트리거끼리
    비슷한 말을 반복하는 걸 막는다. 수면 얘기 한 지 두 시간 만에
    "피곤해 보여요"라고 또 하면 사용자는 알림을 끈다.
    """

    log: SpeechLog
    name: str = "speech_history"
    window: timedelta = timedelta(days=1)

    def fetch(self, insight: Insight, now: datetime) -> Optional[ContextBlock]:
        records = self.log.since(now - self.window)
        if not records:
            return None
        lines = [f"- {r.at:%m/%d %H:%M} ({r.trigger}) {r.text}" for r in records]
        return ContextBlock(label="최근 24시간 동안 내가 한 말", body="\n".join(lines))
