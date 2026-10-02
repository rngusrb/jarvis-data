"""대화 — 사용자가 꺼낸 말에 답한다.

`agent.py` 와 나눠둔 이유는 계기가 반대이기 때문이다. 저쪽은 **신호를 보고
말을 걸지** 정하고(게이트가 대부분을 막는다), 이쪽은 **이미 물어본 것에**
답한다. 사용자가 물었는데 "말 안 걸기로 함"은 말이 안 되므로 게이트가 없다.

대신 싼 길과 비싼 길을 가른다.

    /status  /beliefs  /sleep     DB 조회만. LLM 을 안 부른다
    그 밖의 말                     맥락 조립 → LLM

명령어를 따로 둔 건 흉내가 아니라 **즉답이 쓸모 있기 때문**이다. "지금 뭘
알고 있나"는 자비스가 생각할 필요가 없는 질문이고, 3090을 데우는 동안
기다릴 이유도 없다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Callable, Dict, List, Optional, Sequence

from src.brain.client import Reasoner
from src.brain.context import ContextProvider, assemble, render
from src.channels.base import Incoming
from src.core.beliefs import Status
from src.core.models import Insight, ObservationCatalog, ObservationSource, Severity
from src.storage.beliefs import SQLiteBeliefStore
from src.storage.conversation import SQLiteConversation

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

SYSTEM_PROMPT = """너는 사용자의 개인 비서다. 사용자가 너에게 말을 걸었다.

- 한국어 반말로 짧게 답한다. 세 문장을 넘기지 않는다.
- 아는 것만 말한다. 데이터에 없는 건 없다고 한다.
- 숫자를 나열하지 말고 그래서 어떻다는 것을 말한다.
- 사용자가 네 판단을 부정하면 받아들인다. 우기지 않는다."""


@dataclass
class Conversationalist:
    reasoner: Reasoner
    beliefs: SQLiteBeliefStore
    catalog: ObservationCatalog
    source: ObservationSource
    history: SQLiteConversation
    providers: Sequence[ContextProvider] = ()
    # 대화 맥락으로 끌어올 과거 메시지 수. 길면 프롬프트가 잡음으로 찬다.
    recall: int = 12
    commands: Dict[str, Callable[[datetime], str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.commands = {
            "/status": self._status,
            "/beliefs": self._beliefs,
            "/sleep": self._sleep,
            "/help": self._help,
            "/start": self._help,
        }

    async def reply(self, incoming: Incoming, now: datetime) -> Optional[str]:
        if incoming.is_action:
            # 버튼은 다음 조각에서 다룬다. 지금은 삼키지 말고 알려준다 —
            # 조용히 넘기면 눌렀는데 아무 일도 안 일어난 것처럼 보인다.
            return "그 버튼은 아직 안 붙었어."

        text = incoming.text.strip()
        head = text.split()[0].lower() if text else ""
        if head in self.commands:
            return self.commands[head](now)

        return await self._think(text, now)

    # ── 싼 길 ────────────────────────────────────────

    def _help(self, now: datetime) -> str:
        return (
            "뭐든 물어봐. 명령어도 있어.\n"
            "/status  수집이 잘 되고 있나\n"
            "/sleep   요즘 수면\n"
            "/beliefs 내가 너에 대해 아는 것"
        )

    def _status(self, now: datetime) -> str:
        seen = self.catalog.last_seen()
        if not seen:
            return "아직 아무것도 못 받았어."
        lines = []
        for kind, at in sorted(seen.items()):
            hours = (now - at).total_seconds() / 3600
            mark = "✅" if hours < 36 else "⚠️"
            when = f"{hours:.0f}시간 전" if hours < 48 else f"{hours / 24:.0f}일 전"
            lines.append(f"{mark} {kind} — {when}")
        return "\n".join(lines)

    def _sleep(self, now: datetime) -> str:
        window = self.source.recent("sleep_hours", now - timedelta(days=14))
        rows = sorted(window, key=lambda o: o.at)
        if not rows:
            return "최근 2주 수면 기록이 없어."
        recent = rows[-7:]
        avg = mean(o.value for o in recent)
        body = "\n".join(f"{o.at.astimezone(KST):%m-%d}  {o.value:.1f}시간" for o in recent)
        return f"최근 {len(recent)}번 평균 {avg:.1f}시간.\n{body}"

    def _beliefs(self, now: datetime) -> str:
        known = self.beliefs.all()
        if not known:
            return "아직 아는 게 없어. 흔적이 더 쌓여야 해."
        lines = []
        for b in known:
            days = (now - b.first_seen).days
            mark = {Status.CONFIRMED: "●", Status.CANDIDATE: "○", Status.FADING: "·"}[b.aged(now)]
            lines.append(f"{mark} {b.kind} ({days}일째)\n   {b.value}")
        return "\n".join(lines)

    # ── 비싼 길 ──────────────────────────────────────

    async def _think(self, text: str, now: datetime) -> str:
        parts: List[str] = []

        known = self.beliefs.active(now)
        if known:
            parts.append("[내가 아는 것]\n" + "\n".join(f"  {b.kind} = {b.value}" for b in known))

        # 신호가 없어도 맥락 제공자는 쓸 수 있다. 질문에 답하려면 수집 현황과
        # 관측 추이가 똑같이 필요하기 때문이다.
        probe = Insight(trigger="대화", summary=text, severity=Severity.INFO, at=now)
        blocks = assemble(self.providers, probe, now)
        if blocks:
            parts.append(f"[참고]\n{render(blocks)}")

        past = self.history.recent(self.recall)
        if past:
            parts.append(
                "[최근 대화]\n"
                + "\n".join(f"  {'나' if m.from_user else '자비스'}: {m.text}" for m in past)
            )

        parts.append(f"[지금 질문]\n{text}")
        prompt = "\n\n".join(parts)

        reply = await self.reasoner.ask(prompt, system=SYSTEM_PROMPT)
        if not reply or not reply.strip():
            # 빈 응답을 그대로 돌려주면 사용자는 아무 일도 안 일어난 걸로 본다.
            logger.warning("대화 응답이 비었다: %.60s", text)
            return "미안, 지금 생각이 안 나. 다시 물어봐 줄래?"
        return reply.strip()
