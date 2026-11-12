"""자비스 루프 — 이 프로덕트의 심장.

수집 파이프라인(src/pipelines)이 도는 "배치 루프"와는 별개로 돌아간다.
배치는 데이터를 채우고, 이 루프는 채워진 데이터를 보고 **먼저 말을 건다**.

    최근 관측 조회 → 트리거 감지 → 에이전트 판단 → 채널 발송

루프가 도는 계기는 두 가지다.

  - 주기적으로 (기본 30분): 데이터가 **안** 들어오는 것도 신호다.
    수집이 멈춘 걸 알아채려면 시계가 계속 돌아야 한다.
  - 수집 직후: 아침에 데이터가 도착했는데 30분 뒤에 말을 걸면 늦다.

이 파일에는 판단 로직이 없다. 전부 조립만 한다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from src.brain.agent import JarvisAgent
from src.brain.context import ContextBlock
from src.brain.feedback import buttons, new_token
from src.brain.providers import TopicSource
from src.channels.base import Channel
from src.core.events import EventSource
from src.core.models import Insight, ObservationSource
from src.core.topics import standings
from src.triggers.base import EventTrigger, Trigger, Watch
from src.triggers.topics import TopicWatch, muted

logger = logging.getLogger(__name__)


class Research(Protocol):
    """관심사로 말 걸기 전에 웹에서 찾아 둘 것. 없으면 None."""

    async def prepare(self, insight: Insight, now: datetime) -> Optional[ContextBlock]: ...


@dataclass(frozen=True)
class Preparer:
    """섹터 신호에 붙일 준비 — 신호 이름의 앞부분("train_ahead:5:6:0708:0711" → "train_ahead")으로
    고른다.

    `required` 면 못 찾을 때 말하지 않는다 — 열차 도착 없이 열차 얘기는 쓸모가 없다(JAR-044). 아니면
    찾은 것 없이 판단한다(관심사 준비처럼 — 찾기가 말 걸기를 막지 않는다)."""

    research: Research
    required: bool = False


class Transcript(Protocol):
    """대화 기록. 먼저 건 말도 여기 적어야 사용자가 거기에 답할 때 대화가 이어진다."""

    def add(
        self, who: str, at: datetime, text: str, meta: Optional[Dict[str, Any]] = None
    ) -> None: ...


@dataclass
class JarvisLoop:
    source: ObservationSource
    triggers: Sequence[Trigger]
    agent: JarvisAgent
    channel: Channel
    # 창 없이 "종류별 마지막 수신 시각"을 보는 감시들. 수집 중단처럼 오래될수록
    # 중요해지는 신호는 트리거의 창에 담으면 오래될수록 안 보인다.
    watches: Sequence[Watch] = ()
    snapshot: Optional[Callable[[], Mapping[str, datetime]]] = None
    # 사건(위치 등)을 보는 트리거와 그 재료.
    event_triggers: Sequence[EventTrigger] = ()
    events: Optional[EventSource] = None
    # 관심사 신호와 그 재료(회고가 가꾸는 주제 나무).
    topic_watch: Optional[TopicWatch] = None
    topics: Optional[TopicSource] = None
    # 먼저 건 말을 대화 기록에도 적는다(2026-11-05 사용자 결정). 없으면 발화 기억에만 남는다.
    transcript: Optional[Transcript] = None
    # 관심사 신호가 게이트를 통과하면 판단 전에 웹에서 찾아 둔다(일정을 지어내지 않게). 없으면 안
    # 찾는다.
    research: Optional[Research] = None
    # 섹터 신호에 붙일 준비(신호 이름 앞부분 → 준비). 열차 알림이 판단 전에 실시간 도착을 받는다.
    prepare_for: Mapping[str, Preparer] = field(default_factory=dict)
    # 지금. 관심사 신호는 시간대(깨어 있는 시간)를 보므로 시험에서 정할 수 있어야 한다.
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    # 주기 실행과 수집 훅이 동시에 들어올 수 있다. 겹치면 같은 신호로 두 번
    # 말하게 된다 — 게이트는 **발송에 성공한 뒤에야** 쿨다운을 기록하므로,
    # 나란히 달리는 두 실행은 둘 다 게이트를 통과해버린다.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.event_triggers and self.events is None:
            # 조용히 건너뛰면 트리거가 등록돼 있는데 영영 안 울린다.
            raise ValueError("사건 트리거가 있는데 사건 저장소를 안 넘겼다")
        if self.topic_watch is not None and self.topics is None:
            raise ValueError("관심사 신호가 있는데 주제 나무를 안 넘겼다")

    async def run_once(self) -> int:
        """한 주기를 돈다. 실제로 발송한 메시지 수를 돌려준다."""
        async with self._lock:
            return await self._cycle()

    async def _cycle(self) -> int:
        now = self.clock()
        sent = 0
        # 섹터의 신호(관측치·사건)는 다 모은 뒤 줄기로 묶어 판단한다. 같은 밤을 본 수면 신호 셋이
        # 따로 판단되면 한 주기에 두세 말이 나간다(BACKLOG 4단계).
        heard: List[Insight] = []

        for trigger in self.triggers:
            try:
                window = self.source.recent(trigger.kind, since=now - trigger.lookback)
                insight = trigger.check(window, now)
            except Exception:
                # 트리거 하나가 터졌다고 뒤의 트리거와 수집 감시까지 그 주기를 건너뛰면 안 된다.
                # 2026-11-02 리뷰: 잠든 시각 한 줄이 깨져 있으면 주기 전체가 멈췄다.
                logger.exception("트리거 실패 — 건너뛴다: %s", trigger.name)
                continue
            if insight is not None:
                heard.append(insight)

        if self.events is not None:
            for event_trigger in self.event_triggers:
                try:
                    happened = self.events.recent(event_trigger.kind, now - event_trigger.lookback)
                    insight = event_trigger.check(happened, now)
                except Exception:
                    logger.exception("사건 트리거 실패 — 건너뛴다: %s", event_trigger.name)
                    continue
                if insight is not None:
                    heard.append(insight)

        for lead, rest in self._together(heard, now):
            extra = await self._prepared(lead, now)
            if extra is None:
                continue
            sent += await self._speak(lead, now, rest, extra)

        if self.topic_watch is not None and self.topics is not None:
            try:
                insight = self._topic(now)
            except Exception:
                logger.exception("관심사 신호 실패 — 건너뛴다")
                insight = None
            if insight is not None:
                sent += await self._speak(insight, now, extra=await self._prepare(insight, now))

        if self.watches and self.snapshot is not None:
            try:
                seen = self.snapshot()
            except Exception:
                # 저장소를 못 읽었다고 이번 주기의 다른 말까지 막을 이유는 없다.
                logger.exception("마지막 수신 시각을 읽지 못했다 — 감시를 건너뛴다")
                seen = None
            if seen is not None:
                for watch in self.watches:
                    insight = watch.check(seen, now)
                    if insight is not None:
                        sent += await self._speak(insight, now)

        return sent

    def _together(
        self, heard: Sequence[Insight], now: datetime
    ) -> List[Tuple[Insight, List[Insight]]]:
        """같은 줄기 신호를 (판단할 것, 같이 묶을 것)으로. 줄기가 없는 신호는 혼자다.

        게이트를 통과한 것만 묶는다 — 이미 판단했거나 쉬는 중인 신호를 묶으면 같은 밤을 두 번
        기록한다. 판단할 것은 가장 무거운 신호(같으면 먼저 울린 것)다.
        """
        families: Dict[str, List[Insight]] = {}
        for insight in heard:
            families.setdefault(insight.family or insight.trigger, []).append(insight)
        grouped: List[Tuple[Insight, List[Insight]]] = []
        for members in families.values():
            allowed = [m for m in members if self._allowed(m, now)]
            for member in members:
                if member not in allowed:
                    # 이미 판단했거나 쉬는 중. 묶으면 막힌 신호가 대표가 되어 새 신호까지 판단 없이
                    # 사라진다(JAR-029 리뷰 재현) — 거른 흔적은 남긴다.
                    logger.debug("게이트가 막음 — 묶지 않는다: %s", member.trigger)
            if not allowed:
                continue
            lead = max(allowed, key=lambda m: m.severity)
            grouped.append((lead, [m for m in allowed if m is not lead]))
        return grouped

    def _allowed(self, insight: Insight, now: datetime) -> bool:
        """게이트. 기록을 못 읽으면 그 신호만 이번 주기에 건너뛴다 — 묶기 전에 여기서 터지면 뒤의
        관심사·수집 감시까지 그 주기를 통째로 건너뛴다(판단 안에서 터질 때와 같게 다룬다)."""
        try:
            return self.agent.gate.allows(insight, now)
        except Exception:
            logger.exception("게이트를 못 봤다 — 이번 주기엔 건너뛴다: %s", insight.trigger)
            return False

    def _topic(self, now: datetime) -> Optional[Insight]:
        """뜨는 관심사 중 게이트를 통과하는 첫 것 하나. 한 주기에 관심사 얘기는 하나만 판단한다."""
        assert self.topic_watch is not None and self.topics is not None
        if not self.topic_watch.awake(now):
            return None  # 시간대 밖이면 나무를 읽지도 않는다(수집 훅마다 돈다)
        board = standings(self.topics.load(), self.topics.grounds(), now)
        notes = {
            topic: [n.text for n in found] for topic, found in self.topics.latest_notes(1).items()
        }
        rated = self.agent.gate.log.rated(now - self.topic_watch.mute_for)
        quiet = muted((r.trigger, r.feedback) for r in rated)
        candidates = self.topic_watch.check(board, notes, now, quiet)
        return self.agent.first_allowed(candidates, now)

    async def _prepare(self, insight: Insight, now: datetime) -> Sequence[ContextBlock]:
        """관심사 준비.

        못 찾았거나 터지면 찾아본 것 없이 판단한다 — 찾기가 말 걸기를 막지 않는다.
        """
        if self.research is None:
            return ()
        try:
            block = await self.research.prepare(insight, now)
        except Exception:
            logger.exception("관심사 준비 실패 — 찾아본 것 없이 판단한다: %s", insight.trigger)
            return ()
        return (block,) if block is not None else ()

    async def _prepared(self, insight: Insight, now: datetime) -> Optional[Sequence[ContextBlock]]:
        """섹터 신호의 준비. 꼭 있어야 하는 준비를 못 하면 None — 이번엔 말하지 않는다(판단 기록도
        안 남겨 다음 주기에 다시 본다)."""
        preparer = self.prepare_for.get(insight.trigger.split(":")[0])
        if preparer is None:
            return ()
        try:
            block = await preparer.research.prepare(insight, now)
        except Exception:
            logger.exception("신호 준비 실패: %s", insight.trigger)
            block = None
        if block is not None:
            return (block,)
        if preparer.required:
            logger.info("준비를 못 해 말하지 않는다: %s", insight.trigger)
            return None
        return ()

    async def _speak(
        self,
        insight: Insight,
        now: datetime,
        also: Sequence[Insight] = (),
        extra: Sequence[ContextBlock] = (),
    ) -> int:
        """신호 하나를(같은 줄기의 다른 신호와 함께) 판단하고, 말하기로 하면 보낸다. 보냈으면 1."""
        try:
            message = await self.agent.consider(insight, now, also, extra)
        except Exception:
            # 판단 실패도 발송 실패와 똑같이 다뤄야 한다. 여기서 예외가
            # 위로 새면 이 주기의 **뒤에 선 신호들이 통째로 건너뛰어진다** —
            # 두뇌 하나가 죽었다고 수집 중단 감지까지 멎는 건 말이 안 된다.
            # 실제 사고: vLLM이 로딩되는 10분 동안 아침 신호 4건이 이렇게
            # 사라졌고, 로그에 트레이스백만 남아 아무도 모르고 지나갔다.
            logger.exception("판단 실패 — 건너뛴다: %s", insight.trigger)
            return 0

        if message is None:
            # 기록은 agent.consider 안에서 한다. 여기서는 게이트가 막은
            # 것인지 LLM이 판단한 것인지 구별할 수 없고, 구별 없이
            # 기록하면 쿨다운이 영영 안 끝난다.
            logger.debug("말 안 걸기로 함: %s", insight.trigger)
            return 0

        # 먼저 건 말에는 👍👎 를 단다. 표는 보내기 전에 지어 두고, 보내기에 성공한 뒤에만 기록한다.
        token = new_token()
        try:
            await self.channel.send(message, buttons=buttons(token))
        except Exception:
            # 발송 실패는 루프를 죽이지 않는다. 다음 기회에 다시 시도된다.
            logger.exception("발송 실패 (%s): %s", self.channel.name, insight.trigger)
            return 0

        try:
            self.agent.confirm_spoken(insight, now, message, token=token, also=also)
        except Exception:
            # 말은 나갔다. 기록이 행 여럿(대표 + 묶인 신호)이라 하나가 잠김으로 터질 수 있는데,
            # 여기서 위로 새면 이 주기의 관심사·수집 감시까지 건너뛴다(JAR-029 리뷰). 못 적은
            # 신호는 다음 주기에 다시 판단될 수 있다 — 크게 남긴다.
            logger.exception("보낸 말을 발화 기억에 다 못 적었다: %s", insight.trigger)
        self._transcribe(insight, message, token, also)
        return 1

    def _transcribe(
        self, insight: Insight, message: str, token: str, also: Sequence[Insight] = ()
    ) -> None:
        """먼저 건 말을 대화 기록에도 적는다. 못 적어도 말은 이미 나갔다 — 남기고 넘어간다."""
        if self.transcript is None:
            return
        try:
            self.transcript.add(
                "jarvis",
                self.clock(),
                message,
                meta={
                    "first": True,
                    "trigger": insight.trigger,
                    "token": token,
                    "also": [o.trigger for o in also],
                },
            )
        except Exception:
            # 대화 기록이 빠지면 사용자가 이 말에 답할 때 자비스가 무슨 말에 대한 답인지 모른다.
            # 발화 기억에 남았으면 반복은 안 한다(기록까지 실패했으면 위에서 따로 남겼다).
            logger.exception("먼저 건 말을 대화 기록에 못 적었다: %s", insight.trigger)

    async def run_forever(self, interval_sec: int) -> None:
        while True:
            try:
                sent = await self.run_once()
                logger.info("주기 완료 — %d건 발송", sent)
            except Exception:
                # 한 주기가 통째로 터져도 자비스는 계속 살아 있어야 한다.
                logger.exception("주기 실행 중 예외")
            await asyncio.sleep(interval_sec)
