"""대화 — 사용자가 꺼낸 말에 답한다.

`agent.py` 와 나눠둔 이유는 계기가 반대이기 때문이다. 저쪽은 **신호를 보고
말을 걸지** 정하고(게이트가 대부분을 막는다), 이쪽은 **이미 물어본 것에**
답한다. 사용자가 물었는데 "말 안 걸기로 함"은 말이 안 되므로 게이트가 없다.

대신 싼 길과 비싼 길을 가른다.

    /status  /topics  /sleep      DB 조회만. LLM 을 안 부른다
    /report                        넓게 모아 → LLM 이 보고서를 쓴다(1분쯤)
    그 밖의 말                     맥락 조립 → LLM

명령어를 따로 둔 건 흉내가 아니라 **즉답이 쓸모 있기 때문**이다. "지금 뭘
알고 있나"는 자비스가 생각할 필요가 없는 질문이고, 3090을 데우는 동안
기다릴 이유도 없다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean
from typing import Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Union

from src.brain.client import Reasoner
from src.brain.context import ContextProvider, assemble, render
from src.brain.feedback import MARKS as REACTIONS
from src.brain.feedback import parse
from src.brain.memory import SpeechLog
from src.brain.prompts import is_collection
from src.brain.report import Reporter
from src.channels.base import Incoming
from src.core.clock import local, shown
from src.core.models import ObservationCatalog, ObservationSource
from src.core.topics import TOPIC_FAMILY, Trend, standings
from src.storage.conversation import SQLiteConversation
from src.storage.topics import SQLiteTopicStore

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """너는 사용자의 개인 비서다. 사용자가 너에게 말을 걸었다.

- 한국어 반말로 짧게 답한다. 세 문장을 넘기지 않는다.
- 아는 것만 말한다. 데이터에 없는 건 없다고 한다.
- 숫자를 나열하지 말고 그래서 어떻다는 것을 말한다.
- 사용자가 네 판단을 부정하면 받아들인다. 우기지 않는다."""

# `/topics` 답의 길이. 텔레그램 채널이 긴 말을 나눠 보내긴 하지만, 나무 전체를 여러 통으로 받는 건
# 명령의 즉답이 아니다 — 한 통에 들어가게 자르고 "외 N개"로 (리뷰 측정: 주제 150개면 5,094자).
REPLY_LIMIT = 3500

MARKS = {
    Trend.RISING: "▲",
    Trend.STEADY: "●",
    Trend.NEW: "○",
    Trend.FADING: "▽",
    Trend.QUIET: "·",
}


@dataclass(frozen=True)
class Later:
    """지금 보낼 말과, 시간이 걸려 나중에 보낼 말.

    듣기 루프는 한 건씩 차례로 처리한다. 보고서(1분쯤)를 그 자리에서 기다리면 그동안 `/status` 도
    버튼도 밀리고, 기다리다 한 번 더 누르면 보고서가 두 통 온다(JAR-027 리뷰 재현). 그래서 "쓰는
    중"을 먼저 보내고 보고서는 따로 기다려 보낸다.
    """

    now: str
    then: Awaitable[str]


# 보고서를 쓰는 중이라고 보는 최대 시간. 쓰다가 죽어 표시가 남아도 이만큼 지나면 다시 쓸 수 있다.
WRITING_FOR = timedelta(minutes=15)


@dataclass
class Conversationalist:
    reasoner: Reasoner
    topics: SQLiteTopicStore
    catalog: ObservationCatalog
    source: ObservationSource
    history: SQLiteConversation
    providers: Sequence[ContextProvider] = ()
    # 종류별 끊김 기준(카드의 stale_after). 모르는 종류는 36시간으로 본다.
    stale_after: Mapping[str, timedelta] = field(default_factory=dict)
    # 대화 맥락으로 끌어올 과거 메시지 수. 길면 프롬프트가 잡음으로 찬다.
    recall: int = 12
    commands: Dict[str, Callable[[datetime], str]] = field(default_factory=dict)
    # 자비스가 먼저 건 말의 기억(게이트와 같은 것). 👍👎 를 남기고 `/status` 가 센다.
    speech: Optional[SpeechLog] = None
    # `/report` 를 쓰는 쪽. 없으면 그 명령은 안 붙었다고 답한다.
    reporter: Optional[Reporter] = None
    # 보고서를 쓰기 시작한 때. 쓰는 중에 또 부르면 하나 더 쓰지 않는다.
    _writing_since: Optional[datetime] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.commands = {
            "/status": self._status,
            "/topics": self._topics,
            # 주제 나무 전에는 믿음이었다. 손에 익은 명령을 버리지 않는다.
            "/beliefs": self._topics,
            "/sleep": self._sleep,
            "/help": self._help,
            "/start": self._help,
        }

    async def reply(self, incoming: Incoming, now: datetime) -> Union[str, Later, None]:
        if incoming.is_action:
            return self._react(incoming.action, now)

        text = incoming.text.strip()
        head = text.split()[0].lower() if text else ""
        if head == "/report":
            # 명령어 꼴이지만 생각을 시키는 요청이다 — DB 조회로 답이 안 나온다(brain/_GUIDE).
            return self._report(now)
        if head in self.commands:
            return self.commands[head](now)

        return await self._think(text, now)

    # ── 싼 길 ────────────────────────────────────────

    def _help(self, now: datetime) -> str:
        return (
            "뭐든 물어봐. 명령어도 있어.\n"
            "/status  수집이 잘 되고 있나\n"
            "/sleep   요즘 수면\n"
            "/topics  내가 너에 대해 아는 것(주제 나무)\n"
            "/report  요즘의 너 — 보고서(1분쯤 걸려)"
        )

    def _report(self, now: datetime) -> Union[str, Later]:
        if self.reporter is None:
            return "보고서는 아직 안 붙었어."
        if self._writing_since is not None and now - self._writing_since < WRITING_FOR:
            return "이미 쓰는 중이야. 끝나면 보낼게."
        self._writing_since = now
        return Later(now="쓰는 중이야. 1분쯤 걸려.", then=self._write(self.reporter, now))

    async def _write(self, reporter: Reporter, now: datetime) -> str:
        try:
            return await reporter.write(now)
        finally:
            self._writing_since = None

    def _react(self, action: str, now: datetime) -> str:
        """버튼. 모르는 버튼을 삼키면 눌렀는데 아무 일도 안 일어난 것처럼 보이므로 그대로 말한다."""
        parsed = parse(action)
        if parsed is None or self.speech is None:
            return "그 버튼은 아직 안 붙었어."
        value, token = parsed
        record = self.speech.rate(token, value, now)
        if record is None:
            # 표가 기억에 없다(기록 전에 눌렸거나 DB 가 바뀜). 남긴 척하지 않는다.
            logger.warning("반응을 남길 말이 없다: %s", action)
            return "그 말은 내 기억에 없어서 반응을 못 남겼어."
        if value > 0:
            return f"{REACTIONS[1]} 고마워. 이런 얘기 계속할게."
        if record.trigger.startswith(f"{TOPIC_FAMILY}:"):
            return f"{REACTIONS[-1]} 알겠어. 이 관심사(와 그 아래 가지)는 한 달 동안 안 꺼낼게."
        if is_collection(record.trigger):
            # 수집 경고는 👎 로 재우지 않는다 — 끊김이 길어질수록 조용해지는 감시견이 된다
            # (2026-08-29 사고). 이 👎 의 뜻은 사용자가 정한다(BACKLOG).
            return (
                f"{REACTIONS[-1]} 남겨 뒀어. 다만 수집 경고는 끊김이 이어지면 계속 알려야 해서"
                " 바로 줄이진 않아."
            )
        return f"{REACTIONS[-1]} 알겠어. 이런 얘기는 줄일게."

    def _status(self, now: datetime) -> str:
        seen = self.catalog.last_seen()
        if not seen:
            return "아직 아무것도 못 받았어."
        lines = []
        for kind, at in sorted(seen.items()):
            hours = (now - at).total_seconds() / 3600
            limit = self.stale_after.get(kind, timedelta(hours=36))
            mark = "✅" if now - at < limit else "⚠️"
            when = f"{hours:.0f}시간 전" if hours < 48 else f"{hours / 24:.0f}일 전"
            lines.append(f"{mark} {kind} — {when}")
        if self.speech is not None:
            # 4단계 끝 기준: 사람에 대한 말이 수집 경고보다 많아짐.
            said = self.speech.since(now - timedelta(days=7))
            warned = sum(1 for r in said if is_collection(r.trigger))
            liked = sum(1 for r in said if r.feedback > 0)
            disliked = sum(1 for r in said if r.feedback < 0)
            lines.append(
                f"\n최근 7일 내가 먼저 건 말: 사람 얘기 {len(said) - warned} · 수집 경고 {warned}"
                f" · {REACTIONS[1]} {liked} {REACTIONS[-1]} {disliked}"
            )
        return "\n".join(lines)

    def _sleep(self, now: datetime) -> str:
        window = self.source.recent("sleep_hours", now - timedelta(days=14))
        rows = sorted(window, key=lambda o: o.at)
        if not rows:
            return "최근 2주 수면 기록이 없어."
        recent = rows[-7:]
        avg = mean(o.value for o in recent)
        body = "\n".join(f"{local(o.at):%m-%d}  {o.value:.1f}시간" for o in recent)
        return f"최근 {len(recent)}번 평균 {avg:.1f}시간.\n{body}"

    def _topics(self, now: datetime) -> str:
        board = standings(self.topics.load(), self.topics.grounds(), now)
        if not board:
            return "아직 아는 게 없어. 흔적이 더 쌓여야 해."
        lines: List[str] = []
        size = 0
        for i, s in enumerate(board):
            pad = "  " * s.depth
            line = (
                f"{pad}{MARKS[s.trend]} {s.node.name} — {s.trend.value}"
                f" (근거 {s.count} · {s.days}일)"
            )
            if size + len(line) + 1 > REPLY_LIMIT:
                lines.append(f"… 외 {len(board) - i}개")
                break
            lines.append(line)
            size += len(line) + 1
        return "\n".join(lines)

    # ── 비싼 길 ──────────────────────────────────────

    async def _think(self, text: str, now: datetime) -> str:
        parts: List[str] = []

        # 요즘 관심사(주제 나무)는 맥락 제공자가 넣는다 — 먼저 말 거는 쪽과 같은 것을 본다.
        # 신호 없이 맥락을 조립한다. 가짜 Insight 를 만들어 넘기던 때가
        # 있었는데, 그 가짜는 observations 가 비어서 추이 제공자가 침묵했고
        # 자비스가 "데이터 값이 없어"라고 답했다. 타입을 속이면 거짓말이
        # 아래로 전파된다.
        blocks = assemble(self.providers, None, now)
        if blocks:
            parts.append(f"[참고]\n{render(blocks)}")

        past = self.history.recent(self.recall)
        if past:
            parts.append(
                "[최근 대화]\n"
                + "\n".join(f"  {'나' if m.from_user else '자비스'}: {m.text}" for m in past)
            )

        parts.append(f"[지금] {shown(now)}\n\n[지금 질문]\n{text}")
        prompt = "\n\n".join(parts)

        reply = await self.reasoner.ask(prompt, system=SYSTEM_PROMPT)
        if not reply or not reply.strip():
            # 빈 응답을 그대로 돌려주면 사용자는 아무 일도 안 일어난 걸로 본다.
            logger.warning("대화 응답이 비었다: %.60s", text)
            return "미안, 지금 생각이 안 나. 다시 물어봐 줄래?"
        return reply.strip()
