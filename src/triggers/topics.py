"""관심사 신호 — 주제 나무에서 요즘 뜨는 주제를 "말할 거리" 후보로.

관측치 트리거와 받는 재료가 다르다. 숫자의 추이가 아니라 회고가 매일 가꾸는 나무의 지금
모습(`Standing`)을 받는다. 순수 함수라는 계약은 같다 — 나무를 읽는 건 루프가 한다.

후보를 **여럿** 낸다. 주제마다 따로 "한 번만 판단"이 걸리므로(트리거 이름이 `topic:<번호>`), 맨 앞
하나만 내면 이미 판단한 주제가 매번 앞을 막아 다른 주제는 영영 차례가 안 온다. 어느 것을 판단할지는
게이트가 고른다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src.core.clock import local
from src.core.models import Insight, Severity
from src.core.topics import RECENT_DAYS, Standing, Trend

# 게이트가 "이 줄기로 마지막에 말한 때"를 볼 때 쓰는 줄기 이름. 트리거 이름은 `topic:<번호>`.
FAMILY = "topic"


def topic_trigger(topic: int) -> str:
    return f"{FAMILY}:{topic}"


def muted(reactions: Iterable[Tuple[str, int]]) -> Set[int]:
    """👎 받은 관심사 번호. `reactions` 는 반응이 붙은 말들의 (트리거, 반응) — 창은 부르는 쪽이."""
    found: Set[int] = set()
    for trigger, value in reactions:
        family, _, topic = trigger.partition(":")
        if family == FAMILY and value < 0 and topic.isdigit():
            found.add(int(topic))
    return found


@dataclass(frozen=True)
class TopicWatch:
    name: str = FAMILY
    # 최근 7일 중 이만큼은 근거가 있어야 "요즘"이다. 하루 이틀은 스쳐간 것일 수 있다.
    min_recent_days: int = 3
    # 깨어난 지 이만큼 안이어야 "새로" 뜨는 것이다. 몇 달째 꾸준한 주제는 새 소식이 아니다.
    fresh: timedelta = timedelta(days=14)
    # 말 걸 시간대(한국 시간, 시작 포함·끝 제외). 관심사 얘기는 급하지 않다 — 회고가 새벽 5시 반에
    # 돌고 바로 다음 주기가 6시 전이라, 시간대가 없으면 자는 사람에게 "요즘 자격증 보네"가 간다.
    # 잠든 시각이 새벽 1~3시대, 출근이 7시 반이라(2단계 기록) 출근 뒤부터 잠들기 전까지로 잡는다.
    hours: Tuple[int, int] = (10, 21)
    # 요약에 넣는 최근 근거 수.
    samples: int = 3
    # 👎 받은 관심사는 이만큼 신호를 내지 않는다.
    mute_for: timedelta = timedelta(days=30)

    def check(
        self,
        board: Sequence[Standing],
        notes: Dict[Optional[int], List[str]],
        now: datetime,
        muted: Set[int],
    ) -> List[Insight]:
        """말할 만한 주제들, 앞일수록 강한 것. 시간대 밖이면 빈 목록.

        `notes` 는 주제마다 최근 메모(최근 것부터), `muted` 는 사용자가 👎 한 주제 번호다.
        """
        start, end = self.hours
        if not start <= local(now).hour < end:
            return []
        found: List[Tuple[Tuple[int, datetime], Insight]] = []
        for s in board:
            if s.node.id in muted or s.trend is not Trend.RISING or s.since is None:
                continue
            if s.recent_days < self.min_recent_days or now - s.since > self.fresh:
                continue
            found.append(((s.recent_days, s.since), self._insight(s, notes, now)))
        found.sort(key=lambda pair: pair[0], reverse=True)
        return [insight for _, insight in found]

    def _insight(
        self, s: Standing, notes: Dict[Optional[int], List[str]], now: datetime
    ) -> Insight:
        path = " › ".join(s.path)
        since = s.since or now
        again = " 다시" if s.first is not None and since > s.first else ""
        parts = [
            f"요즘{again} 뜨는 관심사 '{path}' — 최근 {RECENT_DAYS}일 중 {s.recent_days}일, "
            f"근거 {s.count}개(이번엔 {local(since):%m-%d}부터)"
        ]
        latest = notes.get(s.node.id) or []
        if latest:
            parts.append(f"내 메모: {latest[0]}")
        recent = [f"{local(g.at):%m-%d %H:%M} {g.text}" for g in s.own[: self.samples]]
        if recent:
            parts.append("최근 근거: " + " / ".join(recent))
        return Insight(
            trigger=topic_trigger(s.node.id),
            summary="\n".join(parts),
            severity=Severity.NOTABLE,
            # 이 주제가 이번에 깨어난 때. 같은 깨어남은 게이트가 한 번만 판단한다 — 다시 잠잠했다가
            # 깨어나면 새 신호다(Insight.at 계약, triggers/_GUIDE.md).
            at=since,
        )
