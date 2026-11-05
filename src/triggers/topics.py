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
from src.core.topics import RECENT_DAYS, TOPIC_FAMILY, Standing, Trend

# 게이트가 "이 줄기로 마지막에 말한 때"를 볼 때 쓰는 줄기 이름. 트리거 이름은 `topic:<번호>`.
FAMILY = TOPIC_FAMILY


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
    # 최근 7일 중 이만큼은 근거가 있어야 "요즘"이다(하루뿐인 건 "처음 보임"이라 애초에 안 뜬다).
    # 깨어난 지 이만큼 안이어야 "새로" 뜨는 것이다. 몇 달째 꾸준한 주제는 새 소식이 아니다.
    # 백테스트(2026-11-05, 운영 나무로 지난 61일을 매일 15시에): 7일 중 2일·14일 → 신호 15개
    # (주 2개쯤), 3일·14일 → 8개, 4일 → 6개, 2일·21일 → 18개. 초기엔 수다스럽게(2026-10-07 결정).
    # 이 수는 근거가 지금 연결에 붙은 채로 센 하한이다 — 실제로는 회고가 근거를 더 붙여 가며 자란다.
    min_recent_days: int = 2
    fresh: timedelta = timedelta(days=14)
    # 말 걸 시간대(한국 시간, 시작 포함·끝 제외). 관심사 얘기는 급하지 않다 — 회고가 새벽 5시 반에
    # 돌고 바로 다음 주기가 6시 전이라, 시간대가 없으면 자는 사람에게 "요즘 자격증 보네"가 간다.
    # 잠든 시각이 새벽 1~3시대, 출근이 7시 반이라(2단계 기록) 출근 뒤부터 잠들기 전까지로 잡는다.
    hours: Tuple[int, int] = (10, 21)
    # 요약에 넣는 최근 근거 수.
    samples: int = 3
    # 👎 받은 관심사(와 그 아래 가지)는 👎 를 누른 때부터 이만큼 신호를 내지 않는다.
    mute_for: timedelta = timedelta(days=30)

    def awake(self, now: datetime) -> bool:
        start, end = self.hours
        return start <= local(now).hour < end

    def check(
        self,
        board: Sequence[Standing],
        notes: Dict[Optional[int], List[str]],
        now: datetime,
        muted: Set[int],
    ) -> List[Insight]:
        """말할 만한 주제들, 앞일수록 강한 것. 시간대 밖이면 빈 목록.

        `notes` 는 주제마다 최근 메모(최근 것부터), `muted` 는 사용자가 👎 한 주제 번호다 — 그 아래
        가지도 같이 뺀다(줄기를 👎 했는데 새 가지로 같은 얘기가 다시 오면 안 된다).
        줄기와 가지가 함께 뜨면 가지(더 구체적인 쪽)만 낸다. 줄기는 가지의 근거로 깨어난 것이라 같은
        얘기를 두 번 하게 된다(JAR-024 리뷰 재현: '공부'를 말하고 다음 날 '공부 › 자격증').
        """
        if not self.awake(now):
            return []
        parent = {s.node.id: s.node.parent for s in board}
        picked: Dict[int, Standing] = {}
        for s in board:
            if s.trend is not Trend.RISING or s.since is None:
                continue
            if s.recent_days < self.min_recent_days or now - s.since > self.fresh:
                continue
            picked[s.node.id] = s
        found: List[Tuple[Tuple[int, datetime], Insight]] = []
        for topic, s in picked.items():
            if any(_under(other, {topic}, parent) for other in picked if other != topic):
                continue  # 아래에 함께 뜬 가지가 있다 — 그 가지가 말한다
            # 👎 는 "가지만" 거름 **뒤에** 뺀다. 먼저 빼면 👎 받은 가지가 빠진 자리에 그 가지가
            # 깨운 줄기가 같은 근거로 나간다(재리뷰 재현: '공부 › 자격증' 👎 → 다음 날 '공부').
            if _under(topic, muted, parent):
                continue
            found.append(((s.recent_days, s.since or now), self._insight(s, notes, now)))
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
        # 가지까지 합친 최근 근거 — 줄기의 직접 근거는 옛것일 수 있다. 이번에 깨어난 뒤의 것만.
        woke = [g for g in s.latest if g.at >= since]
        recent = [f"{local(g.at):%m-%d %H:%M} {g.text}" for g in woke[: self.samples]]
        if recent:
            parts.append("최근 근거: " + " / ".join(recent))
        return Insight(
            trigger=topic_trigger(s.node.id),
            family=FAMILY,
            summary="\n".join(parts),
            severity=Severity.NOTABLE,
            # 이 주제가 이번에 깨어난 때. 회고가 더 이른 근거를 나중에 붙이면 앞당겨질 수 있어서,
            # 게이트는 "이 때 이후에 판단한 적 있나"로 본다(`SpeechLog.judged_since`).
            # 다시 잠잠했다가 깨어나면 새 신호다(Insight.at 계약, triggers/_GUIDE.md).
            at=since,
        )


def _under(topic: int, roots: Set[int], parent: Dict[int, Optional[int]]) -> bool:
    """`topic` 이 `roots` 중 하나이거나 그 아래 가지인가(살아 있는 나무의 부모 사슬로)."""
    seen: Set[int] = set()
    current: Optional[int] = topic
    while current is not None and current not in seen:
        if current in roots:
            return True
        seen.add(current)
        current = parent.get(current)
    return False
