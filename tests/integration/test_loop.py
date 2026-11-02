from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

import pytest

from src.brain.agent import JarvisAgent
from src.brain.gate import Gate
from src.core.models import Event, Insight, Observation, Severity
from src.runtime.loop import JarvisLoop
from src.sectors.health.triggers import LateSleepTrigger, SleepDropTrigger

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

    async def send(self, text: str) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(text)


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

        async def send(self, text: str) -> None:
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

        async def send(self, text: str) -> None:
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
