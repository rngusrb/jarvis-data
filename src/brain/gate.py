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

from src.brain.memory import InMemorySpeechLog, SpeechLog
from src.core.models import Insight, Severity


@dataclass
class Gate:
    log: SpeechLog = field(default_factory=InMemorySpeechLog)
    min_severity: Severity = Severity.NOTABLE
    cooldown: timedelta = timedelta(hours=6)
    # 트리거마다 적정 재발화 간격이 다르다. 수집 중단처럼 상태가 계속
    # 유지되는 신호는 기본 쿨다운으로 두면 하루 네 번씩 같은 말을 한다.
    cooldown_overrides: Dict[str, timedelta] = field(default_factory=dict)
    # 줄기(`줄기:…` 트리거들)마다, 그 줄기로 마지막에 **말한** 뒤 기다릴 간격. 트리거가 주제마다
    # 따로라도(`topic:12`) 관심사 얘기는 묶어서 센다 — 안 그러면 뜨는 주제 열 개가 한 주기에 열 번
    # 말한다. 말 안 걸기로 한 판단은 줄기 간격을 쓰지 않는다(초기엔 수다스럽게, 2026-10-07 결정).
    family_cooldowns: Dict[str, timedelta] = field(default_factory=dict)

    def cooldown_for(self, trigger: str) -> timedelta:
        return self.cooldown_overrides.get(trigger, self.cooldown)

    def allows(self, insight: Insight, now: datetime) -> bool:
        if insight.severity < self.min_severity:
            return False
        if self.log.judged_since(insight.trigger, insight.at):
            # 이미 판단한 신호다 — 말했든 안 했든, 시간이 지나도 같은 밤은 같은 밤이다.
            # 쿨다운만으로 막을 땐 아침에 들어온 밤을 6시간마다 세 번 물었고, 16시간으로 늘리자
            # 그날 밤 한 번 더 물으면서 다음 날 아침의 새 밤까지 오후로 밀렸다(JAR-018 리뷰 재현).
            # 마지막 기록만 보면 다른 신호가 끼어든 뒤(주제 A, B, 다시 A) 같은 것을 또 묻는다.
            return False
        family = insight.trigger.split(":", 1)[0]
        wait = self.family_cooldowns.get(family)
        if wait is not None and ":" in insight.trigger:
            said = self.log.last_spoken_in(family)
            if said is not None and now - said.at < wait:
                return False
        last = self.log.last(insight.trigger)
        if last is None:
            return True
        return now - last.at >= self.cooldown_for(insight.trigger)
