"""실제 맥락 제공자들.

지금은 이미 손에 있는 정보(관측치 추이, 최근 발화)만 쓴다. 캘린더 파서와
저장소가 생기면 ScheduleProvider, ProfileProvider 같은 게 여기 늘어난다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from src.brain.context import ContextBlock
from src.brain.memory import SpeechLog
from src.core.metrics import Metric
from src.core.models import Insight, ObservationCatalog, ObservationSource


@dataclass
class ObservationTrendProvider:
    """신호에 딸려온 관측치를 추이로 펼쳐준다.

    요약문("평균보다 2시간 짧음")만 주면 모델이 판단할 근거가 얇다.
    실제 숫자 흐름을 보여주면 "3일째 계속 줄고 있네" 같은 말을 할 수 있게 된다.
    """

    name: str = "observation_trend"
    max_points: int = 7

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        # 신호에 딸린 것만 본다. 사용자가 먼저 말을 걸었을 때는 줄 게 없고,
        # 그때 필요한 "최근 값"은 RecentValuesProvider 가 맡는다.
        if insight is None or len(insight.observations) < 2:
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

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
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
    metrics: Sequence[Metric]
    name: str = "collection_status"

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        last_seen = self.catalog.last_seen()
        known = {m.kind: m for m in self.metrics}
        lines: List[str] = []

        for kind in sorted(set(known) | set(last_seen)):
            metric = known.get(kind)
            at = last_seen.get(kind)

            if metric is None:
                state = "수집기 없음 — 아직 만들지 않았다"
            elif not metric.active:
                # 안 만든 것과 일부러 버린 것은 다르다. 섞으면 자비스가
                # 버린 지표를 되살리라고 조른다.
                if at is not None and now - at <= metric.stale_after:
                    # 접었다고 선언해놓고 데이터는 계속 들어오는 상태.
                    # 선언과 현실이 어긋났으니 그대로 말한다.
                    state = "수집 중단했다고 선언됐는데 데이터는 계속 들어온다 — 선언이 낡았다"
                else:
                    state = "수집 중단 — 과거 데이터만 있다. 되살릴 필요 없다"
            elif at is None:
                state = f"{metric.collector} (아직 한 번도 안 들어옴)"
            elif now - at > metric.stale_after:
                hours = (now - at).total_seconds() / 3600
                state = f"{metric.collector} — 그런데 {hours:.0f}시간째 끊김"
            else:
                state = f"{metric.collector} — 정상"

            name = f"{metric.label}({kind})" if metric else kind
            lines.append(f"- {name}: {state}")

        return ContextBlock(label="수집 경로 현황", body="\n".join(lines))


@dataclass
class RecentValuesProvider:
    """지표별 **최근 값을 직접 조회해서** 보여준다.

    `ObservationTrendProvider` 와 재료가 다르다. 저쪽은 신호에 딸려온 것만
    보고, 이쪽은 저장소를 직접 읽는다.

    사고 이력: 2026-10-02. 신호에 딸린 것만 보는 제공자뿐이라, 사용자가
    "데이터 보고 말해봐"라고 세 번 물었는데 자비스가 세 번 다 "값이 없어서
    판단할 수 없어"라고 답했다. 수면도 심박도 위치도 멀쩡히 쌓여 있었다 —
    **프롬프트에 안 들어갔을 뿐이다.**

    접힌(retired) 지표는 뺀다. 과거 데이터만 남은 것을 현재 상태인 양
    보여주면 "걸음수가 646보네"라고 두 달 전 숫자를 읽는다.
    """

    source: ObservationSource
    metrics: Sequence[Metric]
    name: str = "recent_values"
    window: timedelta = timedelta(days=14)
    max_points: int = 7

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        chunks: List[str] = []
        for metric in self.metrics:
            if not metric.active:
                continue
            rows = sorted(self.source.recent(metric.kind, now - self.window), key=lambda o: o.at)
            if not rows:
                continue
            recent = rows[-self.max_points :]
            values = " · ".join(f"{o.at:%m/%d} {o.value:g}" for o in recent)
            chunks.append(f"- {metric.label}({metric.kind}): {values}")

        if not chunks:
            return None
        return ContextBlock(label=f"최근 {self.window.days}일 값", body="\n".join(chunks))
