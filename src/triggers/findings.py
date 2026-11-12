"""알아본 것 신호 — 탐색 루프가 "알릴 것"으로 남긴 것을 말할 거리 후보로(JAR-046).

관심사 신호처럼 후보를 **여럿** 낸다. 알아본 것마다 따로 "한 번만 판단"이 걸리므로(트리거 이름이
`finding:<번호>`), 맨 앞 하나만 내면 이미 판단한 것이 매번 앞을 막는다. 어느 것을 판단할지는
게이트가 고른다. 순수 함수다 — 기록을 읽는 건 루프가 한다.

**같은 날 알아본 것만** 낸다. 말은 판단이 그때 새로 쓰고(저장한 글을 보내지 않는다), 사실의 날짜 옆
거리("4일 뒤")는 지금에 맞춰 코드가 센다 — 옛 생각 루프는 하루 지난 글의 "내일"을 그대로 보냈다
(core/findings 머리말).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Sequence, Tuple

from src.core.clock import local
from src.core.findings import FAMILY, TELL, Finding, finding_trigger, when_label
from src.core.models import Insight, Severity

__all__ = ["FAMILY", "FindingWatch"]

# 판단에 보이는 날짜 원문의 길이 — 말하기 전 판단은 원문을 다 본다.
QUOTE = 200


@dataclass(frozen=True)
class FindingWatch:
    name: str = FAMILY
    # 말 걸 시간대(한국 시간, 시작 포함·끝 제외) — 관심사 신호와 같은 까닭(자는 사람에게 가지 않게).
    hours: Tuple[int, int] = (10, 21)

    def awake(self, now: datetime) -> bool:
        start, end = self.hours
        return start <= local(now).hour < end

    def check(self, findings: Sequence[Finding], now: datetime) -> List[Insight]:
        """말할 만한 것들, 최근 것부터. 시간대 밖이면 빈 목록."""
        if not self.awake(now):
            return []
        today = local(now).date()
        picked = [
            f
            for f in findings
            if f.id is not None
            and f.worth == TELL
            and not f.note
            and f.at <= now
            and local(f.at).date() == today
        ]
        picked.sort(key=lambda f: f.at, reverse=True)
        return [self._insight(f, now) for f in picked]

    def _insight(self, f: Finding, now: datetime) -> Insight:
        assert f.id is not None
        today = local(now).date()
        lines = [
            f"자비스가 시키지 않았는데 스스로 알아본 것 — {f.kind}: {f.subject}"
            f" (알아본 때 {local(f.at):%H:%M})"
        ]
        if f.summary:
            lines.append(f"알게 된 것: {f.summary}")
        for fact in f.facts:
            # 날짜 옆엔 그 날짜가 적혀 있던 원문까지 — 다른 일에 잘못 붙였으면 여기서 드러난다.
            label = when_label(fact, today, quote=QUOTE)
            when = f" — {label}" if label else ""
            source = f" (출처: {fact.source})" if fact.source else ""
            lines.append(f"- {fact.text}{when}{source}")
        if any(fact.no_year for fact in f.facts):
            lines.append(
                "(해 모름: 원문에 해가 확실하지 않다 — 원문 그대로 전하고 언제라고 단정하지 않는다)"
            )
        return Insight(
            trigger=finding_trigger(f.id),
            family=FAMILY,
            summary="\n".join(lines),
            severity=Severity.NOTABLE,
            # 그것을 알아본 때 — 알아본 것 하나는 한 번만 판단한다(Insight.at 계약,
            # triggers/_GUIDE).
            at=f.at,
        )
