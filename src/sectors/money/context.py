"""쓴 돈을 자비스의 맥락에 — 대화("이번 주 얼마 썼어?")와 먼저 말 걸기 판단이 같이 본다.

합계는 코드가 센다. 모델에게 결제 목록을 주고 더하라고 하면 틀린다 — 날짜를 모델이 세게 뒀다가 여섯
번 리뷰에서 막힌 것과 같은 종류다(생각 루프, 2026-11-11). 그래서 "이번 주" 같은 기간도 여기서 정해
숫자로 준다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional, Protocol, Sequence

from src.brain.context import ContextBlock
from src.core.clock import local
from src.core.models import Insight, Trace
from src.sectors.money.payments import SMS, WALLET, merge, read, summarize

LABEL = (
    "쓴 돈 — 카드 승인 문자·애플페이 결제에서 코드가 센 것(같은 결제가 두 길로 오면 "
    "하나로, 취소는 뺐다). 금액은 다시 더하지 말고 이대로 말한다. 여기 없는 결제(현금·다른 "
    "카드)는 모르고, '마지막으로 받은 때'가 오래된 길은 그 뒤를 모른다"
)


class RecentTraces(Protocol):
    def recent(
        self, kind: str, since: datetime, limit: Optional[int] = None
    ) -> Sequence[Trace]: ...


@dataclass
class SpendingProvider:
    source: RecentTraces
    name: str = "spending"
    # 이번 달(31일까지)과 지난주(13일 전까지)를 늘 덮는다.
    window: timedelta = timedelta(days=40)
    # 받기 시작한 날을 찾는 범위. 결제는 하루 몇 건이라 한 해치를 읽어도 가볍다.
    history: timedelta = timedelta(days=400)

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        traces = [t for kind in (SMS, WALLET) for t in self.source.recent(kind, now - self.history)]
        if not traces:
            # 한 해 동안 아무 결제 흔적도 없다 — 자동화를 걸기 전이다. 빈 합계("0원")를 보이면 안
            # 쓴 것으로 읽힌다.
            return None
        reading = read([t for t in traces if t.at >= now - self.window])
        # 받기 시작한 날 — 받은 때와 결제한 때 중 이른 쪽(11/4 23:58 결제를 11/5 00:01 에 받음).
        first = min(
            [local(t.at).date() for t in traces] + [local(p.at).date() for p in reading.payments]
        )
        last: Dict[str, datetime] = {}
        for trace in traces:
            if trace.kind not in last or trace.at > last[trace.kind]:
                last[trace.kind] = trace.at
        lines = summarize(
            merge(reading.payments),
            reading.unread,
            reading.declined,
            first,
            now,
            self.window.days,
            last,
        )
        return ContextBlock(label=LABEL, body="\n".join(lines))
