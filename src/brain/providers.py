"""실제 맥락 제공자들.

지금은 이미 손에 있는 정보(관측치 추이, 최근 발화)만 쓴다. 캘린더 파서와
저장소가 생기면 ScheduleProvider, ProfileProvider 같은 게 여기 늘어난다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Mapping, Optional

from src.brain.context import ContextBlock
from src.brain.memory import SpeechLog
from src.core.models import Insight, ObservationCatalog


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


@dataclass
class CollectionStatusProvider:
    """자비스가 **자기 수집 구조**를 알게 한다.

    이게 없으면 모델은 "걸음수가 며칠째 없다"만 보고 그럴듯한 일반론을 지어낸다.
    실제로 "배터리 최적화를 해제하라"고 답한 적이 있는데, 안드로이드 개념이고
    이 시스템엔 존재하지도 않는 이야기다. 원인은 그냥 걸음수 수집기를 아직
    안 만든 것이었다.

    수집 **방식**은 선언으로 받고(코드가 알 수 없는 설정 사실), 살아 있는지는
    데이터에서 판단한다(선언만 믿으면 낡는다).
    """

    catalog: ObservationCatalog
    # 값이 None이면 '일부러 접은' 지표다. 선언에 아예 없는 것(= 아직 안 만듦)과
    # 구별해야 한다. 안 그러면 자비스가 버린 지표를 만들라고 조른다.
    collectors: Mapping[str, Optional[str]]
    name: str = "collection_status"
    stale_after: timedelta = timedelta(hours=36)

    def fetch(self, insight: Insight, now: datetime) -> Optional[ContextBlock]:
        last_seen = self.catalog.last_seen()
        lines: List[str] = []

        for kind in sorted(set(self.collectors) | set(last_seen)):
            how = self.collectors.get(kind)
            at = last_seen.get(kind)

            if kind not in self.collectors:
                state = "수집기 없음 — 아직 만들지 않았다"
            elif how is None:
                state = "수집 중단 — 과거 데이터만 있다. 되살릴 필요 없다"
            elif at is None:
                state = f"{how} (아직 한 번도 안 들어옴)"
            elif now - at > self.stale_after:
                hours = (now - at).total_seconds() / 3600
                state = f"{how} — 그런데 {hours:.0f}시간째 끊김"
            else:
                state = f"{how} — 정상"
            lines.append(f"- {kind}: {state}")

        return ContextBlock(label="수집 경로 현황", body="\n".join(lines))
