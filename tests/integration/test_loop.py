from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from src.brain.agent import JarvisAgent
from src.brain.gate import Gate
from src.core.models import Event, Insight, Observation, Severity, Trace
from src.core.topics import Op, plan
from src.runtime.loop import JarvisLoop
from src.sectors.health.triggers import (
    ChronicShortSleepTrigger,
    LateSleepTrigger,
    SleepDropTrigger,
)
from src.storage.speech import SQLiteSpeechLog
from src.storage.topics import SQLiteTopicStore
from src.storage.traces import SQLiteTraceStore
from src.triggers.topics import TopicWatch

BASE = datetime(2026, 8, 1, tzinfo=timezone.utc)
KST = timezone(timedelta(hours=9))


@dataclass
class FakeReasoner:
    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        return '{"speak": true, "message": "어젯밤 잘 못 잤네."}'


@dataclass
class RecordingChannel:
    name: str = "recording"
    sent: List[str] = field(default_factory=list)
    delay: float = 0.0
    buttons: List[List[Dict[str, str]]] = field(default_factory=list)

    async def send(self, text: str, buttons: Optional[List[Dict[str, str]]] = None) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(text)
        self.buttons.append(buttons or [])


@dataclass
class FakeSource:
    observations: Sequence[Observation]

    def recent(self, kind: str, since: datetime) -> Sequence[Observation]:
        return [o for o in self.observations if o.kind == kind and o.at >= since]


def _source_with_drop() -> FakeSource:
    # 마지막 밤이 오늘 아침 끝난 밤이어야 판단한다(JAR-018). 밤의 시각은 깬 날의 한국 자정이다.
    today = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    values = [7.0, 7.2, 6.9, 7.1, 3.0]
    return FakeSource(
        [
            Observation(
                source="apple_health",
                kind="sleep_hours",
                value=v,
                at=today - timedelta(days=len(values) - 1 - i),
                meta={"segments": 15},
            )
            for i, v in enumerate(values)
        ]
    )


def _loop(channel: RecordingChannel) -> JarvisLoop:
    return JarvisLoop(
        source=_source_with_drop(),
        triggers=[SleepDropTrigger()],
        agent=JarvisAgent(reasoner=FakeReasoner()),
        channel=channel,
    )


def test_감지되면_발송한다() -> None:
    channel = RecordingChannel()
    assert asyncio.run(_loop(channel).run_once()) == 1
    assert len(channel.sent) == 1


def test_같은_신호를_두_번_말하지_않는다() -> None:
    """두 번째 실행은 쿨다운에 막혀야 한다."""
    channel = RecordingChannel()
    loop = _loop(channel)

    async def twice() -> None:
        await loop.run_once()
        await loop.run_once()

    asyncio.run(twice())
    assert len(channel.sent) == 1


def test_동시에_들어와도_한_번만_말한다() -> None:
    """주기 루프와 수집 훅이 겹치는 상황.

    게이트는 발송에 **성공한 뒤에야** 쿨다운을 기록하므로, 잠금이 없으면
    나란히 달리는 두 실행이 둘 다 게이트를 통과해 같은 말을 두 번 한다.
    """
    channel = RecordingChannel(delay=0.05)
    loop = _loop(channel)

    async def together() -> None:
        await asyncio.gather(loop.run_once(), loop.run_once())

    asyncio.run(together())
    assert len(channel.sent) == 1, f"중복 발화: {channel.sent}"


def test_발송이_터져도_루프는_살아있다() -> None:
    @dataclass
    class BrokenChannel:
        name: str = "broken"

        async def send(self, text: str, buttons: Optional[List[Dict[str, str]]] = None) -> None:
            raise RuntimeError("텔레그램 죽음")

    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[SleepDropTrigger()],
        agent=JarvisAgent(reasoner=FakeReasoner()),
        channel=BrokenChannel(),
    )
    assert asyncio.run(loop.run_once()) == 0


@dataclass
class BrokenReasoner:
    """두뇌가 죽어 있는 상태. vLLM 로딩 중이 정확히 이렇다."""

    calls: int = 0

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.calls += 1
        raise ConnectionError("Server disconnected without sending a response.")


@dataclass
class AlwaysFires:
    """무조건 신호를 내는 트리거. 순서 효과만 보려고 판단 로직을 뺐다."""

    name: str
    kind: str = "sleep_hours"
    lookback: timedelta = timedelta(days=21)

    def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]:
        return Insight(
            trigger=self.name,
            summary=f"{self.name} 신호",
            severity=Severity.URGENT,
            at=now,
        )


def test_broken_brain_does_not_silence_later_triggers() -> None:
    """실제 사고의 재현.

    2026-08-27 아침, vLLM이 로딩되는 10분 동안 판단이 예외로 터졌다.
    그 예외가 _cycle 밖으로 새면서 **뒤에 선 트리거들은 검사조차 되지
    않았다**. 두뇌가 죽었다고 수집 중단 감지까지 멎으면 안 된다.
    """
    reasoner = BrokenReasoner()
    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[AlwaysFires("first"), AlwaysFires("second"), AlwaysFires("third")],
        agent=JarvisAgent(reasoner=reasoner),
        channel=RecordingChannel(),
    )

    sent = asyncio.run(loop.run_once())

    assert sent == 0
    # 핵심: 첫 트리거에서 멈추지 않고 셋 다 판단까지 갔다.
    assert reasoner.calls == 3


def test_broken_brain_leaves_no_cooldown_behind() -> None:
    """말하지 못한 신호는 "말했다"로 기록되면 안 된다.

    기록되면 두뇌가 살아난 뒤에도 쿨다운 내내 침묵한다.
    """
    agent = JarvisAgent(reasoner=BrokenReasoner())
    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[AlwaysFires("chronic_short_sleep")],
        agent=agent,
        channel=RecordingChannel(),
    )
    asyncio.run(loop.run_once())
    assert agent.gate.log.last("chronic_short_sleep") is None


@dataclass
class SkippingReasoner:
    """무조건 말 안 걸기로 판정하는 두뇌."""

    calls: int = 0

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.calls += 1
        return "SKIP"


def test_skip_consumes_the_cooldown() -> None:
    """사고 재현 — 2026-09-29.

    재시작 후 20시간 동안 LLM을 68번 부르고 2번 말했다. 게이트가 쿨다운을
    넘겨 통과시키면 LLM이 SKIP을 내놓는데, 그 판정을 아무 데도 안 남겨서
    30분 뒤 똑같이 물어봤다. **"싼 게이트 먼저, 비싼 LLM 나중"이 새고 있었다.**
    """
    reasoner = SkippingReasoner()
    agent = JarvisAgent(reasoner=reasoner)
    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[AlwaysFires("sleep_drop")],
        agent=agent,
        channel=RecordingChannel(),
    )

    # 세 주기를 연달아 돈다. 쿨다운(기본 6시간) 안이므로 LLM은 한 번만
    # 불려야 한다.
    for _ in range(3):
        asyncio.run(loop.run_once())

    assert reasoner.calls == 1


def test_skip_does_not_pollute_what_jarvis_said() -> None:
    """SKIP 판정이 맥락에 섞이면 자비스가 하지도 않은 말을 했다고 착각한다."""
    agent = JarvisAgent(reasoner=SkippingReasoner())
    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[AlwaysFires("sleep_drop")],
        agent=agent,
        channel=RecordingChannel(),
    )
    asyncio.run(loop.run_once())

    # 게이트는 판단 시각을 안다.
    last = agent.gate.log.last("sleep_drop")
    assert last is not None and last.spoken is False
    # 맥락 제공자는 못 본다.
    assert agent.gate.log.since(BASE) == []


def test_send_failure_still_retries() -> None:
    """발송 실패는 판정이 아니라 사고다. 다시 시도돼야 한다.

    이걸 SKIP 과 같이 취급하면, 사용자는 메시지를 못 받았는데 자비스는
    "아까 말했지" 하고 쿨다운 내내 침묵한다.
    """

    @dataclass
    class BrokenChannel:
        name: str = "broken"

        async def send(self, text: str, buttons: Optional[List[Dict[str, str]]] = None) -> None:
            raise ConnectionError("발송 실패")

    reasoner = FakeReasoner()
    agent = JarvisAgent(reasoner=reasoner)
    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[AlwaysFires("sleep_drop")],
        agent=agent,
        channel=BrokenChannel(),
    )
    asyncio.run(loop.run_once())
    asyncio.run(loop.run_once())

    # 아무것도 기록되지 않았으므로 다음 기회에 다시 시도된다.
    assert agent.gate.log.last("sleep_drop") is None


def test_gate_block_does_not_slide_the_cooldown() -> None:
    """사고 재현 — 2026-09-30.

    SKIP 을 기록하게 만들면서 **게이트가 막은 경우까지** 같이 기록했다.
    그러면 막힐 때마다 새 기록이 쌓이고 쿨다운 시계가 계속 앞으로 밀려서
    영영 말을 못 한다. 실제로 SKIP 52건이 0~9분 간격으로 쌓였고, 마지막
    발화 이후 이틀간 한마디도 못 했다.

    기록해야 할 건 **LLM의 판단**뿐이다. 게이트가 막은 건 이미 끝난 일이다.
    """
    reasoner = SkippingReasoner()
    agent = JarvisAgent(reasoner=reasoner)
    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[AlwaysFires("sleep_drop")],
        agent=agent,
        channel=RecordingChannel(),
    )

    asyncio.run(loop.run_once())
    first = agent.gate.log.last("sleep_drop")
    assert first is not None

    # 쿨다운 안에서 여러 번 더 돈다. 게이트가 막으므로 기록이 늘면 안 된다.
    for _ in range(5):
        asyncio.run(loop.run_once())

    still = agent.gate.log.last("sleep_drop")
    assert still is not None
    assert still.at == first.at, "게이트가 막았는데 기록이 갱신됐다 — 쿨다운이 밀린다"
    assert reasoner.calls == 1


def test_cooldown_actually_expires() -> None:
    """쿨다운이 지나면 다시 판단해야 한다. 밀리기 시작하면 이게 깨진다.

    신호의 `at` 은 판단 시각이다(수집 감시처럼 상태가 이어지는 신호). 같은 `at` 의 신호는
    쿨다운이 지나도 다시 묻지 않는다 — 같은 밤은 같은 밤이다(JAR-018).
    """
    reasoner = SkippingReasoner()
    agent = JarvisAgent(reasoner=reasoner, gate=Gate(cooldown=timedelta(hours=6)))

    def ongoing(now: datetime) -> Insight:
        return Insight(trigger="collection", summary="테스트", severity=Severity.URGENT, at=now)

    assert asyncio.run(agent.consider(ongoing(BASE), BASE)) is None
    assert reasoner.calls == 1

    # 쿨다운 안 — 게이트가 막고 LLM은 안 불린다.
    later = BASE + timedelta(hours=1)
    assert asyncio.run(agent.consider(ongoing(later), later)) is None
    assert reasoner.calls == 1

    # 쿨다운 밖 — 다시 판단한다.
    after = BASE + timedelta(hours=7)
    assert asyncio.run(agent.consider(ongoing(after), after)) is None
    assert reasoner.calls == 2


@dataclass
class OnceWatch:
    """마지막 수신 시각을 받아 늘 같은 신호를 내는 감시."""

    name: str = "collection"
    seen: List[dict] = field(default_factory=list)

    def check(self, last_seen: dict, now: datetime) -> Optional[Insight]:
        self.seen.append(dict(last_seen))
        return Insight(trigger=self.name, summary="수집이 끊겼다", severity=Severity.URGENT, at=now)


def test_감시가_마지막_수신_시각을_받아_말한다() -> None:
    watch = OnceWatch()
    channel = RecordingChannel()
    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[],
        agent=JarvisAgent(reasoner=FakeReasoner()),
        channel=channel,
        watches=[watch],
        snapshot=lambda: {"location": BASE},
    )
    assert asyncio.run(loop.run_once()) == 1
    assert watch.seen == [{"location": BASE}]
    assert channel.sent == ["어젯밤 잘 못 잤네."]


def test_마지막_수신_시각을_못_읽어도_다른_신호는_말한다() -> None:
    def broken() -> dict:
        raise RuntimeError("DB 잠김")

    channel = RecordingChannel()
    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[AlwaysFires("sleep_drop")],
        agent=JarvisAgent(reasoner=FakeReasoner()),
        channel=channel,
        watches=[OnceWatch()],
        snapshot=broken,
    )
    assert asyncio.run(loop.run_once()) == 1
    assert len(channel.sent) == 1


@dataclass
class SeenEvents:
    """받은 사건 창을 기록하고 늘 같은 신호를 내는 사건 트리거."""

    name: str = "unusual_boarding"
    kind: str = "location"
    lookback: timedelta = timedelta(days=42)
    seen: List[int] = field(default_factory=list)

    def check(self, events: Sequence[Event], now: datetime) -> Optional[Insight]:
        self.seen.append(len(events))
        return Insight(
            trigger=self.name, summary="다른 곳에서 탔다", severity=Severity.NOTABLE, at=now
        )


class FakeEvents:
    def __init__(self, events: List[Event]) -> None:
        self.events = events
        self.asked: List[str] = []

    def recent(self, kind: str, since: datetime) -> List[Event]:
        self.asked.append(kind)
        return [e for e in self.events if e.kind == kind and e.at >= since]


def test_사건_트리거가_사건_창을_받아_말한다() -> None:
    now = datetime.now(timezone.utc)
    events = FakeEvents([Event(source="s", kind="location", at=now - timedelta(hours=1))])
    trigger = SeenEvents()
    channel = RecordingChannel()
    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[],
        agent=JarvisAgent(reasoner=FakeReasoner()),
        channel=channel,
        event_triggers=[trigger],
        events=events,
    )

    assert asyncio.run(loop.run_once()) == 1
    assert trigger.seen == [1] and events.asked == ["location"]


def test_사건_트리거만_있고_사건_저장소가_없으면_시작하지_않는다() -> None:
    import pytest

    # 조용히 건너뛰면 트리거가 등록돼 있는데 영영 안 울린다.
    with pytest.raises(ValueError):
        JarvisLoop(
            source=_source_with_drop(),
            triggers=[],
            agent=JarvisAgent(reasoner=FakeReasoner()),
            channel=RecordingChannel(),
            event_triggers=[SeenEvents()],
        )


class Exploding:
    name = "broken"
    kind = "sleep_hours"
    lookback = timedelta(days=1)

    def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]:
        raise ValueError("잠든 시각이 깨졌다")


def test_트리거_하나가_터져도_뒤의_트리거와_감시는_돈다() -> None:
    watch = OnceWatch()
    channel = RecordingChannel()
    loop = JarvisLoop(
        source=_source_with_drop(),
        triggers=[Exploding(), AlwaysFires("sleep_drop")],
        agent=JarvisAgent(reasoner=FakeReasoner()),
        channel=channel,
        watches=[watch],
        snapshot=lambda: {"location": BASE},
    )

    assert asyncio.run(loop.run_once()) >= 1
    assert watch.seen  # 감시까지 갔다


@dataclass
class CountingSkip:
    """매번 말 안 걸기로 하는 두뇌. 몇 번 불렸는지만 센다."""

    calls: int = 0

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.calls += 1
        return '{"speak": false}'


def _short_night(day: int) -> Observation:
    wake = datetime(2026, 11, 3, tzinfo=KST) + timedelta(days=day)
    return Observation(
        source="apple_health",
        kind="sleep_hours",
        value=3.0,
        at=wake,
        meta={"segments": 15, "onset": (wake + timedelta(hours=4)).isoformat()},
    )


def _usual_nights() -> List[Observation]:
    out = []
    for day in range(-7, 0):
        wake = datetime(2026, 11, 3, tzinfo=KST) + timedelta(days=day)
        out.append(
            Observation(
                source="apple_health",
                kind="sleep_hours",
                value=7.5,
                at=wake,
                meta={"segments": 15, "onset": (wake - timedelta(hours=1)).isoformat()},
            )
        )
    return out


@pytest.mark.parametrize("arrivals", [(7.5, 7.5, 7.5), (23.0, 6.5, 6.5), (15.0, 7.0, 3.5)])
def test_같은_밤은_한_번만_묻고_다음_밤은_들어오자마자_묻는다(arrivals: tuple) -> None:
    """사흘 연속 짧고 늦은 밤. 30분마다 판단하고, 두뇌는 매번 말 안 걸기로 한다.

    쿨다운만으로 막을 땐 아침에 들어온 밤을 6시간마다 세 번 물었고, 16시간이면 그날 밤 한 번 더
    물으면서 다음 날 새 밤이 오후로 밀렸다(JAR-018 리뷰 재현).
    """
    first = datetime(2026, 11, 3, tzinfo=KST)
    for trigger in (SleepDropTrigger(), LateSleepTrigger()):
        reasoner = CountingSkip()
        agent = JarvisAgent(reasoner=reasoner, gate=Gate())
        judged: dict = {}
        now = first
        while now < first + timedelta(days=3):
            window = _usual_nights() + [
                _short_night(d)
                for d in range(3)
                if now >= first + timedelta(days=d, hours=arrivals[d])
            ]
            insight = trigger.check(window, now.astimezone(timezone.utc))
            if insight is not None:
                before = reasoner.calls
                asyncio.run(agent.consider(insight, now.astimezone(timezone.utc)))
                if reasoner.calls > before:
                    judged.setdefault(insight.at, []).append(now)
            now += timedelta(minutes=30)

        assert [len(v) for v in judged.values()] == [1, 1, 1], (trigger.name, judged)
        for day, times in enumerate(judged.values()):
            arrived = first + timedelta(days=day, hours=arrivals[day])
            assert times[0] - arrived < timedelta(minutes=30), (trigger.name, day, times)


# ── 관심사로 먼저 말 걸기 (JAR-024·025) ──────────────────────────────


def _topic_loop(tmp_path: Path, channel: RecordingChannel, now: datetime) -> JarvisLoop:
    """요즘 뜨는 주제 둘(사흘·나흘)이 있는 나무와, 관심사 얘기를 반나절에 한 번 하는 게이트."""
    db = tmp_path / "t.db"
    traces = SQLiteTraceStore(db)
    traces.write(
        [
            Trace(
                source="mac_chrome", kind="web_visit", text=f"{who}{d}", at=now - timedelta(days=d)
            )
            for who, days in (("자격증", (0.5, 1.5, 2.5)), ("해커톤", (0.5, 1.5, 2.5, 3.5)))
            for d in days
        ]
    )
    found = traces.recent("web_visit", now - timedelta(days=10))
    ids = {t.text: t.id for t in found if t.id is not None}
    topics = SQLiteTopicStore(db)
    ops = [
        Op(
            "create",
            name=name,
            evidence=tuple(i for text, i in ids.items() if text.startswith(name)),
        )
        for name in ("자격증", "해커톤")
    ]
    topics.apply(plan(topics.load(), ops, set(ids.values()), now), now)
    gate = Gate(log=SQLiteSpeechLog(db), family_cooldowns={"topic": timedelta(hours=12)})
    return JarvisLoop(
        source=FakeSource([]),
        triggers=[],
        agent=JarvisAgent(reasoner=FakeReasoner(), gate=gate),
        channel=channel,
        topic_watch=TopicWatch(),
        topics=topics,
        clock=lambda: now,
    )


AFTERNOON = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)  # 한국 15:00


def test_뜨는_관심사로_한_번에_하나만_먼저_말을_건다(tmp_path: Path) -> None:
    channel = RecordingChannel()
    loop = _topic_loop(tmp_path, channel, AFTERNOON)

    assert asyncio.run(loop.run_once()) == 1
    assert [b["text"] for b in channel.buttons[0]] == ["👍", "👎"]
    said = loop.agent.gate.log.last_spoken_in("topic")
    assert said is not None and said.trigger == "topic:2"  # 나흘째인 해커톤이 먼저
    # 표가 버튼과 기록에 같이 남는다 — 누르면 이 말을 찾아간다.
    assert said.token is not None and said.token in channel.buttons[0][0]["action"]

    # 30분 뒤: 다른 주제(자격증)도 뜨지만 관심사 얘기는 반나절에 한 번이다.
    loop.clock = lambda: AFTERNOON + timedelta(minutes=30)
    assert asyncio.run(loop.run_once()) == 0


def test_엄지_아래를_받은_관심사는_다음_차례에서_빠진다(tmp_path: Path) -> None:
    channel = RecordingChannel()
    loop = _topic_loop(tmp_path, channel, AFTERNOON)
    asyncio.run(loop.run_once())
    said = loop.agent.gate.log.last_spoken_in("topic")
    assert said is not None and said.token is not None
    loop.agent.gate.log.rate(said.token, -1, AFTERNOON + timedelta(minutes=5))

    # 다음 날 한국 10:00 — 줄기 간격(12시간)이 지났고 깨어 있는 시간이다.
    candidate = loop._topic(AFTERNOON + timedelta(hours=19))
    assert candidate is not None and candidate.trigger == "topic:1"


def test_자는_시간에는_관심사_얘기를_꺼내지_않는다(tmp_path: Path) -> None:
    channel = RecordingChannel()
    dawn = datetime(2026, 11, 4, 20, 43, tzinfo=timezone.utc)  # 한국 05:43 — 회고 직후 첫 주기
    assert asyncio.run(_topic_loop(tmp_path, channel, dawn).run_once()) == 0
    assert channel.sent == []


@dataclass
class CountingSkipper:
    """매번 말 안 걸기로 하고, 몇 번 물었는지 센다."""

    calls: int = 0

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.calls += 1
        return '{"speak": false}'


def _tree_loop(tmp_path: Path, reasoner: object, now: datetime) -> tuple:
    """빈 나무와 관심사 신호를 단 루프. (루프, 흔적 저장소, 주제 저장소)."""
    db = tmp_path / "t.db"
    gate = Gate(log=SQLiteSpeechLog(db), family_cooldowns={"topic": timedelta(hours=6)})
    topics = SQLiteTopicStore(db)
    loop = JarvisLoop(
        source=FakeSource([]),
        triggers=[],
        agent=JarvisAgent(reasoner=reasoner, gate=gate),  # type: ignore[arg-type]
        channel=RecordingChannel(),
        topic_watch=TopicWatch(),
        topics=topics,
        clock=lambda: now,
    )
    return loop, SQLiteTraceStore(db), topics


def _grow_topic(
    traces: SQLiteTraceStore,
    topics: SQLiteTopicStore,
    name: str,
    days_ago: Sequence[float],
    now: datetime,
    parent: Optional[int] = None,
    into: Optional[int] = None,
) -> int:
    """흔적을 쓰고 회고처럼 새 주제로 만들거나(`into` 없음) 있는 주제에 붙인다. 주제 번호."""
    texts = [f"{name}-{d}" for d in days_ago]
    traces.write(
        [
            Trace(source="mac_chrome", kind="web_visit", text=t, at=now - timedelta(days=d))
            for t, d in zip(texts, days_ago)
        ]
    )
    ids = [
        t.id
        for t in traces.recent("web_visit", now - timedelta(days=60))
        if t.text in texts and t.id is not None
    ]
    op = (
        Op("attach", topic=into, evidence=tuple(ids))
        if into is not None
        else Op("create", name=name, parent=parent, evidence=tuple(ids))
    )
    topics.apply(plan(topics.load(), [op], set(ids), now), now)
    return into if into is not None else max(n.id for n in topics.load().live())


def test_더_이른_근거가_나중에_붙어도_같은_깨어남은_다시_보내지_않는다(tmp_path: Path) -> None:
    """회고가 다음 날 더 이른 흔적을 붙이면 깨어난 때가 앞당겨진다.

    글자만 보면 다시 보냈다(리뷰 재현).
    """
    day1 = datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)  # 한국 10:00
    loop, traces, topics = _tree_loop(tmp_path, FakeReasoner(), day1)
    topic = _grow_topic(traces, topics, "자격증", (1.5, 0.5), day1)
    assert asyncio.run(loop.run_once()) == 1

    day2 = day1 + timedelta(days=1)
    _grow_topic(traces, topics, "자격증이전", (4.5,), day2, into=topic)  # 11-01 흔적을 붙임
    loop.clock = lambda: day2
    assert asyncio.run(loop.run_once()) == 0


def test_한_주기에는_관심사_하나만_판단한다(tmp_path: Path) -> None:
    """후보가 여럿이어도 LLM 은 한 번 — 말 안 걸기로 하면 다음 후보는 다음 주기에."""
    now = datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)
    skipper = CountingSkipper()
    loop, traces, topics = _tree_loop(tmp_path, skipper, now)
    _grow_topic(traces, topics, "자격증", (0.5, 1.5), now)
    _grow_topic(traces, topics, "해커톤", (0.5, 1.5, 2.5), now)

    asyncio.run(loop.run_once())
    assert skipper.calls == 1
    loop.clock = lambda: now + timedelta(minutes=30)
    asyncio.run(loop.run_once())
    assert skipper.calls == 2
    loop.clock = lambda: now + timedelta(hours=1)
    asyncio.run(loop.run_once())
    assert skipper.calls == 2  # 둘 다 판단했다


def test_줄기에_엄지_아래를_받으면_새_가지로도_다시_안_온다(tmp_path: Path) -> None:
    """회사마다 가지가 는다 — 줄기 👎 가 그 가지 하나만 막으면 같은 얘기가 새 가지로 온다."""
    day1 = datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)
    loop, traces, topics = _tree_loop(tmp_path, FakeReasoner(), day1)
    trunk = _grow_topic(traces, topics, "취업", (0.5, 1.5), day1)
    assert asyncio.run(loop.run_once()) == 1
    said = loop.agent.gate.log.last_spoken_in("topic")
    assert said is not None and said.token is not None
    loop.agent.gate.log.rate(said.token, -1, day1 + timedelta(minutes=5))

    # 열흘 뒤(👎 30일 안): 새 회사 가지가 뜬다. 가지는 줄기와 다른 신호라 판단 기록도 없다 —
    # 막는 건 👎 뿐이다.
    later = day1 + timedelta(days=10)
    _grow_topic(traces, topics, "금융결제원", (0.5, 1.5), later, parent=trunk)
    loop.clock = lambda: later
    assert asyncio.run(loop.run_once()) == 0


# ── 줄기로 묶기, 먼저 건 말을 대화 기록에 (JAR-029·030) ──────────────────────


@dataclass
class Fixed:
    """늘 같은 신호를 내는 트리거. 창은 안 본다."""

    insight: Insight
    kind: str = "sleep_hours"
    lookback: timedelta = timedelta(days=1)

    @property
    def name(self) -> str:
        return self.insight.trigger

    def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]:
        return self.insight


@dataclass
class Answering:
    reply: str = '{"speak": true, "message": "어젯밤 짧게, 늦게 잤네."}'
    prompts: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.prompts.append(prompt)
        return self.reply


@dataclass
class Transcript:
    added: List[tuple] = field(default_factory=list)
    broken: bool = False

    def add(self, who: str, at: datetime, text: str, meta: Optional[dict] = None) -> None:
        if self.broken:
            raise RuntimeError("database is locked")
        self.added.append((who, at, text, meta or {}))


NIGHT = datetime(2026, 11, 5, tzinfo=KST)
MORNING = NIGHT + timedelta(hours=8)


def _signal(trigger: str, summary: str, severity: Severity, family: str = "") -> Insight:
    return Insight(trigger=trigger, summary=summary, severity=severity, at=NIGHT, family=family)


def _grouping_loop(
    signals: List[Insight], reasoner: Answering, transcript: Optional[Transcript] = None
) -> tuple:
    channel = RecordingChannel()
    loop = JarvisLoop(
        source=FakeSource([]),
        triggers=[Fixed(s) for s in signals],
        agent=JarvisAgent(reasoner=reasoner, gate=Gate()),
        channel=channel,
        clock=lambda: MORNING,
        transcript=transcript,
    )
    return loop, channel


def test_같은_줄기_신호는_한_판단_한_말로_묶는다() -> None:
    """같은 밤을 본 수면 신호들이 한 주기에 울리면 따로 판단돼 두 말이 나갔다(BACKLOG 4단계).

    가장 무거운 신호를 판단하고 나머지는 같은 판단에 넣는다. 묶인 신호도 판단한 것으로 남아
    다음 주기에 따로 묻지 않는다. 말한 것은 하나로 센다.
    """
    drop = _signal("sleep_drop", "어젯밤 수면 4.0시간", Severity.NOTABLE, "health")
    late = _signal("late_sleep", "평소보다 2시간 늦게 잠듦", Severity.URGENT, "health")
    reasoner = Answering()
    loop, channel = _grouping_loop([drop, late], reasoner)

    assert asyncio.run(loop.run_once()) == 1
    assert len(channel.sent) == 1 and len(reasoner.prompts) == 1
    prompt = reasoner.prompts[0]
    assert "감지된 신호: 평소보다 2시간 늦게 잠듦" in prompt
    assert "같은 줄기의 다른 신호" in prompt and "어젯밤 수면 4.0시간" in prompt

    log = loop.agent.gate.log
    lead, joined = log.last("late_sleep"), log.last("sleep_drop")
    assert lead is not None and lead.spoken and lead.family == "health"
    assert joined is not None and not joined.spoken and "함께 판단: late_sleep" in joined.text
    assert [r.trigger for r in log.since(NIGHT)] == ["late_sleep"]

    assert asyncio.run(loop.run_once()) == 0
    assert len(reasoner.prompts) == 1


def test_묶어서_말_안_하기로_하면_모두_그_판단으로_남는다() -> None:
    drop = _signal("sleep_drop", "어젯밤 수면 4.0시간", Severity.NOTABLE, "health")
    late = _signal("late_sleep", "평소보다 2시간 늦게 잠듦", Severity.NOTABLE, "health")
    reasoner = Answering(reply='{"speak": false}')
    loop, channel = _grouping_loop([drop, late], reasoner)

    assert asyncio.run(loop.run_once()) == 0
    log = loop.agent.gate.log
    assert log.judged_since("sleep_drop", NIGHT) and log.judged_since("late_sleep", NIGHT)
    # 먼저 울린 것이 판단을 맡는다(무게가 같으면).
    assert "감지된 신호: 어젯밤 수면 4.0시간" in reasoner.prompts[0]

    assert asyncio.run(loop.run_once()) == 0
    assert len(reasoner.prompts) == 1 and channel.sent == []


def test_줄기가_다르거나_없으면_묶지_않는다() -> None:
    """잠과 출근은 다른 얘기다. 줄기 없는 신호는 혼자 판단한다."""
    signals = [
        _signal("sleep_drop", "어젯밤 수면 4.0시간", Severity.NOTABLE, "health"),
        _signal("late_departure", "평소보다 40분 늦게 나섬", Severity.NOTABLE, "commute"),
        _signal("solo", "줄기 없는 신호", Severity.NOTABLE),
    ]
    reasoner = Answering()
    loop, channel = _grouping_loop(signals, reasoner)

    assert asyncio.run(loop.run_once()) == 3
    assert len(reasoner.prompts) == 3
    assert all("같은 줄기의 다른 신호" not in p for p in reasoner.prompts)


def test_먼저_건_말도_대화_기록에_적는다() -> None:
    """사용자가 먼저 건 말에 답하면 대화가 이어져야 한다(2026-11-05 사용자 결정)."""
    transcript = Transcript()
    loop, channel = _grouping_loop(
        [_signal("sleep_drop", "어젯밤 수면 4.0시간", Severity.NOTABLE, "health")],
        Answering(),
        transcript,
    )

    assert asyncio.run(loop.run_once()) == 1
    assert len(transcript.added) == 1
    who, at, text, meta = transcript.added[0]
    assert who == "jarvis" and at == MORNING and text == channel.sent[0]
    assert meta["first"] is True and meta["trigger"] == "sleep_drop"
    said = loop.agent.gate.log.last("sleep_drop")
    assert said is not None and meta["token"] == said.token


def test_대화_기록에_못_적어도_말은_남고_시끄럽게_실패한다(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript = Transcript(broken=True)
    loop, channel = _grouping_loop(
        [_signal("sleep_drop", "어젯밤 수면 4.0시간", Severity.NOTABLE, "health")],
        Answering(),
        transcript,
    )

    assert asyncio.run(loop.run_once()) == 1
    said = loop.agent.gate.log.last("sleep_drop")
    assert said is not None and said.spoken
    assert "대화 기록에 못 적었다" in caplog.text


def test_묶다가_게이트가_터져도_그_신호만_건너뛴다(caplog: pytest.LogCaptureFixture) -> None:
    """묶기 전에 게이트(기록 읽기)가 터지면 뒤의 줄기와 감시까지 그 주기를 통째로 건너뛰었다."""

    @dataclass
    class Flaky(Gate):
        def allows(self, insight: Insight, now: datetime) -> bool:
            if insight.trigger == "sleep_drop":
                raise RuntimeError("database is locked")
            return super().allows(insight, now)

    reasoner = Answering()
    loop = JarvisLoop(
        source=FakeSource([]),
        triggers=[
            Fixed(_signal("sleep_drop", "어젯밤 수면 4.0시간", Severity.NOTABLE, "health")),
            Fixed(_signal("late_departure", "40분 늦게 나섬", Severity.NOTABLE, "commute")),
        ],
        agent=JarvisAgent(reasoner=reasoner, gate=Flaky()),
        channel=RecordingChannel(),
        clock=lambda: MORNING,
    )

    assert asyncio.run(loop.run_once()) == 1
    assert "40분 늦게 나섬" in reasoner.prompts[0]
    assert "게이트를 못 봤다" in caplog.text


def test_실제_수면_트리거들이_같은_밤에_울리면_한_말이다() -> None:
    """수면 급감과 늦게 잠듦이 같은 밤을 보고 한 주기에 울린다(BACKLOG 4단계의 본 버그).

    손으로 줄기를 단 신호가 아니라 섹터 트리거 그대로다 — 트리거가 줄기를 빼먹으면 여기서 걸린다.
    """
    reasoner = Answering()
    channel = RecordingChannel()
    loop = JarvisLoop(
        source=FakeSource(_usual_nights() + [_short_night(0)]),
        triggers=[SleepDropTrigger(), LateSleepTrigger(), ChronicShortSleepTrigger()],
        agent=JarvisAgent(reasoner=reasoner, gate=Gate()),
        channel=channel,
        clock=lambda: datetime(2026, 11, 3, 8, 0, tzinfo=KST),
    )

    assert asyncio.run(loop.run_once()) == 1
    assert len(reasoner.prompts) == 1 and len(channel.sent) == 1
    assert "같은 줄기의 다른 신호" in reasoner.prompts[0]
    log = loop.agent.gate.log
    judged = [log.last(t) for t in ("sleep_drop", "late_sleep")]
    assert all(r is not None and r.family == "health" for r in judged)
    assert sum(1 for r in judged if r is not None and r.spoken) == 1


def test_게이트에_막힌_신호는_묶지_않는다() -> None:
    """이미 판단한 무거운 신호를 묶으면 그게 대표가 되어 판단 안에서 막히고, 같이 묶인 새 밤의
    수면 급감까지 판단도 기록도 없이 사라졌다(JAR-029 리뷰 재현 — 만성 부족은 7일에 한 번)."""
    chronic = _signal("chronic_short_sleep", "요즘 내내 짧게 잠", Severity.URGENT, "health")
    drop = _signal("sleep_drop", "어젯밤 수면 4.0시간", Severity.NOTABLE, "health")
    reasoner = Answering()
    loop, channel = _grouping_loop([chronic, drop], reasoner)
    loop.agent.gate.log.record(
        "chronic_short_sleep",
        NIGHT + timedelta(hours=1),
        "말 안 걸기로 함",
        spoken=False,
        about=NIGHT,
        family="health",
    )

    assert asyncio.run(loop.run_once()) == 1
    assert len(reasoner.prompts) == 1
    prompt = reasoner.prompts[0]
    assert "감지된 신호: 어젯밤 수면 4.0시간" in prompt and "요즘 내내 짧게 잠" not in prompt


def test_보낸_말을_기록하다_터져도_주기의_나머지는_돈다(caplog: pytest.LogCaptureFixture) -> None:
    """기록이 대표·묶인 신호 여러 행이라 하나가 잠김으로 터질 수 있다. 위로 새면 이 주기의 다른
    줄기·관심사·수집 감시까지 건너뛰었다(JAR-029 리뷰)."""
    from src.brain.memory import InMemorySpeechLog

    @dataclass
    class Locked(InMemorySpeechLog):
        def record(
            self,
            trigger: str,
            at: datetime,
            text: str,
            spoken: bool = True,
            about: Optional[datetime] = None,
            token: Optional[str] = None,
            family: str = "",
        ) -> None:
            if not spoken:
                raise RuntimeError("database is locked")
            super().record(trigger, at, text, spoken, about, token, family)

    reasoner = Answering()
    channel = RecordingChannel()
    loop = JarvisLoop(
        source=FakeSource([]),
        triggers=[
            Fixed(_signal("sleep_drop", "어젯밤 수면 4.0시간", Severity.URGENT, "health")),
            Fixed(_signal("late_sleep", "평소보다 2시간 늦게 잠듦", Severity.NOTABLE, "health")),
            Fixed(_signal("late_departure", "40분 늦게 나섬", Severity.NOTABLE, "commute")),
        ],
        agent=JarvisAgent(reasoner=reasoner, gate=Gate(log=Locked())),
        channel=channel,
        clock=lambda: MORNING,
    )

    assert asyncio.run(loop.run_once()) == 2
    assert len(channel.sent) == 2
    assert "보낸 말을 발화 기억에 다 못 적었다" in caplog.text


# ── 관심사 준비: 말 걸기 전에 찾아 두기 (JAR-033) ─────────────────────


@dataclass
class Prompted:
    prompts: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.prompts.append(prompt)
        return '{"speak": true, "message": "자격증 시험이 다가오네."}'


@dataclass
class FoundIt:
    body: str = "· 정처기 3회 실기 11.13 까지 (큐넷)"
    seen: List[str] = field(default_factory=list)
    broken: bool = False

    async def prepare(self, insight: Insight, now: datetime) -> Any:
        from src.brain.context import ContextBlock

        self.seen.append(insight.trigger)
        if self.broken:
            raise RuntimeError("searx down")
        return ContextBlock(label="찾아본 것(웹, 방금 — 자료일 뿐이다)", body=self.body)


def test_a_topic_is_researched_before_it_is_judged(tmp_path: Path) -> None:
    """판단은 \"다가오는 일정을 하나 곁들인다\" — 아는 것만으로 쓰면 일정을 지어낼 수 있다."""
    channel = RecordingChannel()
    loop = _topic_loop(tmp_path, channel, AFTERNOON)
    reasoner = Prompted()
    loop.agent.reasoner = reasoner
    loop.research = FoundIt()

    assert asyncio.run(loop.run_once()) == 1
    assert loop.research.seen and loop.research.seen[0].startswith("topic:")
    assert (
        "찾아본 것(웹, 방금" in reasoner.prompts[0] and "11.13 까지 (큐넷)" in reasoner.prompts[0]
    )


def test_a_failed_research_does_not_stop_the_topic(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    channel = RecordingChannel()
    loop = _topic_loop(tmp_path, channel, AFTERNOON)
    loop.research = FoundIt(broken=True)

    assert asyncio.run(loop.run_once()) == 1
    assert "관심사 준비 실패" in caplog.text


def test_only_topics_are_researched() -> None:
    """수면·출근 신호는 기록에 대한 것이다 — 웹에서 찾을 게 없다."""
    research = FoundIt()
    loop, _ = _grouping_loop(
        [_signal("sleep_drop", "어젯밤 수면 4.0시간", Severity.NOTABLE, "health")], Answering()
    )
    loop.research = research
    assert asyncio.run(loop.run_once()) == 1
    assert research.seen == []
