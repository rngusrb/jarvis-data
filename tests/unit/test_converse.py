"""대화 응답 — 싼 길과 비싼 길.

명령어는 LLM 을 안 불러야 한다. "지금 뭘 알고 있나"는 자비스가 생각할
필요가 없는 질문이고, 3090 을 데우는 동안 기다릴 이유도 없다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

from src.brain.converse import Conversationalist
from src.brain.providers import TopicProvider
from src.channels.base import Incoming
from src.core.models import Observation, Trace
from src.core.topics import Op, plan
from src.storage.conversation import SQLiteConversation
from src.storage.speech import SQLiteSpeechLog
from src.storage.sqlite import SQLiteStore
from src.storage.topics import SQLiteTopicStore
from src.storage.traces import SQLiteTraceStore

NOW = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)


@dataclass
class CountingReasoner:
    calls: int = 0
    answer: str = "요즘 잠이 좀 부족해 보여."
    prompts: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return self.answer


def _build(tmp_path: Path, reasoner: Optional[CountingReasoner] = None) -> Conversationalist:
    db = tmp_path / "t.db"
    store = SQLiteStore(db)
    store.write(
        [
            Observation(
                source="apple_health",
                kind="sleep_hours",
                value=v,
                at=NOW - timedelta(days=d),
                meta={"segments": 16},
            )
            for d, v in ((1, 5.16), (0, 3.06))
        ]
    )
    return Conversationalist(
        reasoner=reasoner or CountingReasoner(),
        topics=SQLiteTopicStore(db),
        catalog=store,
        source=store,
        history=SQLiteConversation(db),
        providers=(TopicProvider(topics=SQLiteTopicStore(db)),),
        speech=SQLiteSpeechLog(db),
    )


def _grow(tmp_path: Path, c: Conversationalist) -> None:
    """이틀에 걸쳐 근거가 붙은 주제 하나(뜨는 중)."""
    traces = SQLiteTraceStore(tmp_path / "t.db")
    traces.write(
        [
            Trace(source="mac_chrome", kind="web_visit", text=text, at=NOW - timedelta(days=d))
            for d, text in ((2, "토스 채용"), (2, "KT 대졸신입"), (1, "자소서 항목"))
        ]
    )
    ids = [t.id for t in traces.recent("web_visit", NOW - timedelta(days=3)) if t.id is not None]
    tree = c.topics.load()
    ops = [
        Op("create", ref="a", name="채용준비", evidence=tuple(ids)),
        Op("note", topic="a", text="AI 직무 지원 중"),
    ]
    c.topics.apply(plan(tree, ops, set(ids), NOW), NOW)


def _ask(c: Conversationalist, text: str) -> Optional[str]:
    return asyncio.run(c.reply(Incoming(text=text, at=NOW, cursor=1), NOW))


def test_명령어는_llm을_부르지_않는다(tmp_path: Path) -> None:
    reasoner = CountingReasoner()
    c = _build(tmp_path, reasoner)
    for command in ("/status", "/sleep", "/topics", "/beliefs", "/help"):
        assert _ask(c, command)
    assert reasoner.calls == 0


def test_sleep_명령이_실제_값을_보여준다(tmp_path: Path) -> None:
    answer = _ask(_build(tmp_path), "/sleep")
    assert answer is not None
    assert "3.1시간" in answer or "3.1" in answer
    assert "평균" in answer


def test_status_는_끊긴_것을_표시한다(tmp_path: Path) -> None:
    answer = _ask(_build(tmp_path), "/status")
    assert answer is not None and "sleep_hours" in answer


def test_아는_게_없으면_없다고_한다(tmp_path: Path) -> None:
    answer = _ask(_build(tmp_path), "/topics")
    assert answer is not None and "아직 아는 게 없어" in answer


def test_topics_는_나무를_보여준다(tmp_path: Path) -> None:
    """옛 /beliefs 도 같은 답이다 — 손에 익은 명령을 버리지 않는다."""
    c = _build(tmp_path)
    _grow(tmp_path, c)
    answer = _ask(c, "/topics")
    assert answer is not None and "▲ 채용준비 — 뜨는 중 (근거 3 · 2일)" in answer
    assert _ask(c, "/beliefs") == answer


def test_자유_질문은_llm을_부른다(tmp_path: Path) -> None:
    reasoner = CountingReasoner()
    answer = _ask(_build(tmp_path, reasoner), "요즘 나 어때?")
    assert answer == "요즘 잠이 좀 부족해 보여."
    assert reasoner.calls == 1


def test_요즘_관심사가_프롬프트에_들어간다(tmp_path: Path) -> None:
    reasoner = CountingReasoner()
    c = _build(tmp_path, reasoner)
    _grow(tmp_path, c)
    _ask(c, "나 요즘 뭐 하고 있지?")
    assert "채용준비 — 뜨는 중(최근 7일 중 2일): AI 직무 지원 중" in reasoner.prompts[-1]


def test_대화에도_지금을_알려준다(tmp_path: Path) -> None:
    # "어제 맥 뭐 했지?"에 답하려면 지금이 언제인지 알아야 한다. 01:00 UTC 는 한국 10:00 이다.
    reasoner = CountingReasoner()
    _ask(_build(tmp_path, reasoner), "어제 뭐 했지?")
    assert "[지금] 2026-10-02(금) 10:00" in reasoner.prompts[-1]


def test_빈_응답을_그대로_돌려주지_않는다(tmp_path: Path) -> None:
    """빈 문자열을 보내면 사용자는 아무 일도 안 일어난 걸로 본다."""
    answer = _ask(_build(tmp_path, CountingReasoner(answer="   ")), "뭐해?")
    assert answer is not None and answer.strip()


def test_아직_안_붙은_버튼은_알려준다(tmp_path: Path) -> None:
    c = _build(tmp_path)
    answer = asyncio.run(
        c.reply(Incoming(text="x", at=NOW, cursor=1, action="useless:sleep_drop"), NOW)
    )
    assert answer is not None and "아직" in answer


def test_상태는_합친_수신과_카드별_기준으로_말한다(tmp_path: Path) -> None:
    """위치를 사건 표로 옮긴 뒤에도 /status 에 보이고, 36시간 고정이 아니라 카드 기준이다."""
    from datetime import timedelta

    class Merged:
        def last_seen(self) -> dict:
            return {"location": NOW - timedelta(hours=50), "sleep_hours": NOW - timedelta(hours=50)}

    c = _build(tmp_path)
    c.catalog = Merged()
    c.stale_after = {"location": timedelta(days=4)}

    answer = _ask(c, "/status") or ""

    assert "✅ location" in answer  # 위치는 나흘까지 정상
    assert "⚠️ sleep_hours" in answer  # 카드 기준을 모르면 36시간


def test_topics_fits_in_one_telegram_message(tmp_path: Path) -> None:
    """텔레그램은 4,096자가 넘는 메시지를 거절한다. 그러면 사용자는 아무 답도 못 받는다."""
    c = _build(tmp_path)
    traces = SQLiteTraceStore(tmp_path / "t.db")
    traces.write(
        [
            Trace(
                source="mac_chrome", kind="web_visit", text=f"흔적 {i}", at=NOW - timedelta(hours=i)
            )
            for i in range(400)
        ]
    )
    ids = sorted(t.id for t in traces.recent("web_visit", NOW - timedelta(days=30)) if t.id)
    ops = [
        Op("create", name=f"꽤 긴 이름의 주제 번호 {i}", evidence=(ids[2 * i], ids[2 * i + 1]))
        for i in range(200)
    ]
    c.topics.apply(plan(c.topics.load(), ops, set(ids), NOW), NOW)

    answer = _ask(c, "/topics")

    assert answer is not None and len(answer) <= 4096
    assert answer.splitlines()[-1].startswith("… 외 ")


def _press(c: Conversationalist, action: str) -> Optional[str]:
    return asyncio.run(c.reply(Incoming(text="", at=NOW, cursor=1, action=action), NOW))


def test_엄지를_누르면_그_말에_반응이_남는다(tmp_path: Path) -> None:
    c = _build(tmp_path)
    assert c.speech is not None
    # 관심사인지는 기록의 줄기로 안다 — 이름을 쪼개 짐작하지 않는다(JAR-029).
    c.speech.record("topic:7", NOW, "요즘 정보처리기사 보네", token="t7", family="topic")
    c.speech.record("late_sleep", NOW, "어젯밤 늦게 잤네", token="s1", family="health")

    assert "이 관심사(와 그 아래 가지)는 한 달 동안 안 꺼낼게" in (_press(c, "fb:-:t7") or "")
    assert "계속할게" in (_press(c, "fb:+:s1") or "")
    assert [(r.trigger, r.feedback) for r in c.speech.rated(NOW - timedelta(days=1))] == [
        ("topic:7", -1),
        ("late_sleep", 1),
    ]


def test_수집_경고의_엄지_아래는_감시견을_재우지_않는다고_말한다(tmp_path: Path) -> None:
    """2026-08-29: 끊김이 길어질수록 감시견이 조용해졌다. 👎 하나로 그렇게 되면 안 된다."""
    c = _build(tmp_path)
    assert c.speech is not None
    c.speech.record("collection", NOW, "폰 데이터가 끊겼어", token="w1")
    assert "바로 줄이진 않아" in (_press(c, "fb:-:w1") or "")


def test_기억에_없는_말의_엄지는_남긴_척하지_않는다(tmp_path: Path) -> None:
    assert "반응을 못 남겼어" in (_press(_build(tmp_path), "fb:+:없는표") or "")


def test_상태에_먼저_건_말을_사람_얘기와_수집_경고로_센다(tmp_path: Path) -> None:
    """4단계 끝 기준: 사람에 대한 말이 수집 경고보다 많아짐."""
    c = _build(tmp_path)
    assert c.speech is not None
    c.speech.record("collection", NOW - timedelta(days=1), "폰 데이터가 끊겼어", token="a")
    c.speech.record("stale_data:sleep_hours", NOW - timedelta(days=2), "수면이 안 와", token="b")
    c.speech.record("topic:3", NOW - timedelta(hours=3), "요즘 자격증 보네", token="c")
    c.speech.record("late_sleep", NOW - timedelta(days=9), "옛날 얘기", token="d")
    c.speech.record("topic:4", NOW, "말 안 걸기로 함", spoken=False)
    c.speech.rate("c", 1, NOW)

    answer = _ask(c, "/status") or ""
    assert "최근 7일 내가 먼저 건 말: 사람 얘기 1 · 수집 경고 2 · 👍 1 👎 0" in answer


def test_report는_쓰는_중이라고_먼저_답하고_나중에_보고서를_낸다(tmp_path: Path) -> None:
    """1분 동안 듣기 루프를 막지 않는다.

    쓰는 중에 또 부르면 하나 더 쓰지 않는다(리뷰 재현: 두 통).
    """
    from src.brain.converse import Later

    @dataclass
    class FakeReporter:
        calls: List[datetime] = field(default_factory=list)

        async def write(self, now: datetime) -> str:
            self.calls.append(now)
            return "1. 요즘의 너"

    c = _build(tmp_path)
    assert "아직 안 붙었어" in str(_ask(c, "/report"))
    reporter = FakeReporter()
    c.reporter = reporter  # type: ignore[assignment]

    first = asyncio.run(c.reply(Incoming(text="/report", at=NOW, cursor=1), NOW))
    assert isinstance(first, Later) and "쓰는 중" in first.now
    again = asyncio.run(c.reply(Incoming(text="/report", at=NOW, cursor=2), NOW))
    assert again == "이미 쓰는 중이야. 끝나면 보낼게."

    async def finish() -> str:
        assert isinstance(first, Later)
        return await first.then

    assert asyncio.run(finish()) == "1. 요즘의 너" and reporter.calls == [NOW]
    # 다 썼으면 다시 쓸 수 있다.
    third = asyncio.run(c.reply(Incoming(text="/report", at=NOW, cursor=3), NOW))
    assert isinstance(third, Later)
    asyncio.run(finish_later(third))
    assert "/report" in str(_ask(c, "/help"))


async def finish_later(later: object) -> str:
    from src.brain.converse import Later

    assert isinstance(later, Later)
    return await later.then


# ── 웹을 찾아보고 답하기 (JAR-031~033) ─────────────────────────────


@dataclass
class Turns:
    """도구 고리에 정해 둔 차례를 차례로 준다."""

    turns: list
    asked: list = field(default_factory=list)

    async def chat(self, messages, tools=None, thinking=None):  # type: ignore[no-untyped-def]
        self.asked.append(messages)
        return self.turns.pop(0)


@dataclass
class OneResult:
    async def search(self, query: str) -> list:
        from src.brain.web import Found

        return [
            Found(
                title="큐넷",
                url="https://www.q-net.or.kr/a",
                snippet="3회 실기 10.24~",
                engine="naver",
            )
        ]


def _with_tools(tmp_path: Path, turns: list) -> tuple:
    from src.brain.tools import ToolLoop

    c = _build(tmp_path)
    client = Turns(turns)
    c.tools = ToolLoop(client=client, search=OneResult(), reader=None)  # type: ignore[arg-type]
    return c, client


def test_a_plain_question_is_answered_at_once_even_with_tools(tmp_path: Path) -> None:
    from src.brain.client import Turn
    from src.brain.tools import GUIDE

    c, client = _with_tools(tmp_path, [Turn(content="나는 잘 있어. 너는?")])
    assert _ask(c, "안녕") == "나는 잘 있어. 너는?"
    system = client.asked[0][0]["content"]
    assert GUIDE in system and "한국어 반말" in system


def test_a_question_that_needs_the_web_says_it_is_looking_first(tmp_path: Path) -> None:
    """찾기 시작하면 10~40초 — \"찾아볼게\"를 먼저 보내고 답은 따로 보낸다(듣기 루프의 Later)."""
    from src.brain.client import ToolCall, Turn
    from src.brain.converse import LOOKING, Later

    call = ToolCall(
        id="c1", name="web_search", arguments={"query": "정처기 실기"}, raw={"id": "c1"}
    )
    c, _ = _with_tools(
        tmp_path,
        [Turn(content="", calls=(call,)), Turn(content="3회 실기는 10월 24일부터야 (큐넷).")],
    )
    reply = asyncio.run(c.reply(Incoming(text="정처기 실기 언제야?", at=NOW, cursor=1), NOW))

    assert isinstance(reply, Later) and reply.now == LOOKING
    assert asyncio.run(_await(reply.then)) == "3회 실기는 10월 24일부터야 (큐넷)."


async def _await(then):  # type: ignore[no-untyped-def]
    return await then


def test_an_unreachable_brain_still_gets_an_answer(tmp_path: Path) -> None:
    """예외를 위로 올리면 듣기 루프가 로그만 남기고 아무 답도 안 보낸다 — 무슨 일인지 말한다."""
    import httpx

    from src.brain.converse import DOWN, STUCK

    @dataclass
    class Raising:
        error: Exception

        async def chat(self, messages, tools=None, thinking=None):  # type: ignore[no-untyped-def]
            raise self.error

    from src.brain.tools import ToolLoop

    for error, expected in (
        (httpx.ConnectError("down", request=httpx.Request("POST", "http://brain")), DOWN),
        (RuntimeError("bad"), STUCK),
    ):
        c = _build(tmp_path)
        c.tools = ToolLoop(client=Raising(error), search=OneResult(), reader=None)  # type: ignore[arg-type]
        assert _ask(c, "정처기 언제야?") == expected


def test_a_slow_search_is_stopped_and_said(tmp_path: Path, monkeypatch: Any) -> None:
    """상한이 없으면 \"찾아볼게\"만 가고 답도 로그도 안 올 수 있었다(리뷰 재현)."""
    from datetime import timedelta as td

    import src.brain.converse as converse
    from src.brain.client import ToolCall, Turn

    @dataclass
    class Stuck(Turns):
        async def chat(self, messages, tools=None, thinking=None):  # type: ignore[no-untyped-def]
            if self.asked:
                await asyncio.sleep(5)
            return await super().chat(messages, tools, thinking)

    monkeypatch.setattr(converse, "ANSWER_LIMIT", td(milliseconds=50))
    from src.brain.tools import ToolLoop

    call = ToolCall(id="c1", name="web_search", arguments={"query": "q"}, raw={"id": "c1"})
    c = _build(tmp_path)
    c.tools = ToolLoop(
        client=Stuck([Turn(content="", calls=(call,)), Turn(content="늦은 답")]),
        search=OneResult(),
        reader=None,
    )  # type: ignore[arg-type]
    reply = asyncio.run(c.reply(Incoming(text="q", at=NOW, cursor=1), NOW))
    assert isinstance(reply, converse.Later)
    assert asyncio.run(_await(reply.then)) == converse.TOO_LONG


def test_an_empty_first_turn_does_not_claim_to_search(tmp_path: Path) -> None:
    """빈 답이라 한 번 더 묻는 것뿐이면 \"찾아볼게\"는 거짓말이다(리뷰)."""
    from src.brain.client import Turn
    from src.brain.converse import THINKING, Later

    c, _ = _with_tools(tmp_path, [Turn(content=""), Turn(content="다시 생각해 보니 이래.")])
    reply = asyncio.run(c.reply(Incoming(text="음?", at=NOW, cursor=1), NOW))
    assert isinstance(reply, Later) and reply.now == THINKING
    assert asyncio.run(_await(reply.then)) == "다시 생각해 보니 이래."


# ── 답한 뒤 스스로 돌아보기 (JAR-039·040) ─────────────────────────────

JUDGED = '{"result": "일부", "blocked": "페이지를 못 읽음", "note": "빈 틀", "missing": "브라우저"}'


@dataclass
class Holding:
    """돌아보기 판단 — 풀어 줄 때까지 붙잡는다(답이 그것을 기다리지 않는지 보려고)."""

    reply: str = JUDGED
    error: Optional[Exception] = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    prompts: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.prompts.append(prompt)
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.reply


def _checked(c: Conversationalist, tmp_path: Path, judge: Holding) -> Any:
    from src.brain.selfcheck import GapReport, SelfReview
    from src.storage.selfchecks import SQLiteSelfChecks

    checks = SQLiteSelfChecks(tmp_path / "t.db")
    c.selfcheck = SelfReview(reasoner=judge, store=checks)  # type: ignore[arg-type]
    c.gaps = GapReport(reasoner=judge, store=checks)  # type: ignore[arg-type]
    return checks


async def _settled(c: Conversationalist) -> None:
    await asyncio.gather(*list(c._checks))


def test_the_answer_goes_out_first_and_is_looked_back_on_after(tmp_path: Path) -> None:
    """사용자 결정: "답은 기존처럼 내뱉고 나중에 기록해" — 돌아보기가 답을 붙잡지 않는다."""
    c = _build(tmp_path, CountingReasoner(answer="공지엔 없어."))
    judge = Holding()
    checks = _checked(c, tmp_path, judge)

    async def run() -> tuple:
        # 돌아보기를 기다렸다면 여기서 멈춘다(풀어 주기 전이다) — 멈추지 말고 실패하게 시간을 건다.
        answer = await asyncio.wait_for(
            c.reply(Incoming(text="정처기 실기 언제야?", at=NOW, cursor=1), NOW), 2
        )
        await asyncio.wait_for(judge.started.wait(), 1)
        before = checks.since(NOW - timedelta(days=1))
        judge.release.set()
        await _settled(c)
        return answer, before

    answer, before = asyncio.run(run())
    assert answer == "공지엔 없어." and before == []
    [kept] = checks.since(NOW - timedelta(days=1))
    assert (kept.result, kept.question, kept.answer) == (
        "일부",
        "정처기 실기 언제야?",
        "공지엔 없어.",
    )
    assert "(도구를 안 썼다)" in judge.prompts[0]


def test_what_the_tools_did_goes_into_the_look_back(tmp_path: Path) -> None:
    from src.brain.client import ToolCall, Turn
    from src.brain.converse import Later

    call = ToolCall(
        id="c1", name="web_search", arguments={"query": "정처기 실기"}, raw={"id": "c1"}
    )
    c, _ = _with_tools(
        tmp_path, [Turn(content="", calls=(call,)), Turn(content="10월 24일부터야.")]
    )
    judge = Holding()
    judge.release.set()
    checks = _checked(c, tmp_path, judge)

    async def run() -> str:
        reply = await c.reply(Incoming(text="정처기 실기 언제야?", at=NOW, cursor=1), NOW)
        assert isinstance(reply, Later)
        answer = await reply.then
        await _settled(c)
        return answer

    assert asyncio.run(run()) == "10월 24일부터야."
    assert "- 검색 정처기 실기 → 1개" in judge.prompts[0]
    # 지금 있는 기능도 같이 — 모르면 이미 있는 기능을 "있었으면"으로 적었다.
    assert "- web_search: 웹을 검색한다" in judge.prompts[0] and "- read_page:" in judge.prompts[0]
    assert checks.since(NOW - timedelta(days=1))[0].tools[0][:3] == ("search", "정처기 실기", True)


def test_a_timeout_or_blank_answer_is_kept_as_failed_without_asking(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from datetime import timedelta as td

    import src.brain.converse as converse
    from src.brain.client import ToolCall, Turn
    from src.brain.tools import ToolLoop

    @dataclass
    class Stuck(Turns):
        async def chat(self, messages, tools=None, thinking=None):  # type: ignore[no-untyped-def]
            if self.asked:
                await asyncio.sleep(5)
            return await super().chat(messages, tools, thinking)

    monkeypatch.setattr(converse, "ANSWER_LIMIT", td(milliseconds=50))
    call = ToolCall(id="c1", name="web_search", arguments={"query": "q"}, raw={"id": "c1"})
    c = _build(tmp_path, CountingReasoner(answer="   "))
    judge = Holding()
    checks = _checked(c, tmp_path, judge)
    c.tools = ToolLoop(
        client=Stuck([Turn(content="", calls=(call,)), Turn(content="늦은 답")]),
        search=OneResult(),
        reader=None,
    )  # type: ignore[arg-type]
    reply = asyncio.run(c.reply(Incoming(text="오래 걸릴 질문", at=NOW, cursor=1), NOW))
    assert isinstance(reply, converse.Later)
    assert asyncio.run(_await(reply.then)) == converse.TOO_LONG

    c.tools = None
    assert _ask(c, "빈 답이 올 질문") == converse.BLANK
    kept = checks.since(NOW - timedelta(days=1))
    # 멈출 때까지 쓴 도구도 남는다 — 무엇에서 걸렸나.
    assert kept[0].tools and kept[0].tools[0][0] == "search"
    assert [(k.question, k.result, k.blocked) for k in kept] == [
        ("오래 걸릴 질문", "못 함", "도구 한도·시간"),
        ("빈 답이 올 질문", "못 함", "기타"),
    ]
    assert judge.prompts == []


def test_a_look_back_that_breaks_is_said_in_the_log_only(tmp_path: Path, caplog: Any) -> None:
    c = _build(tmp_path, CountingReasoner(answer="답"))
    judge = Holding(error=RuntimeError("down"))
    judge.release.set()
    checks = _checked(c, tmp_path, judge)

    async def run() -> Any:
        answer = await c.reply(Incoming(text="질문", at=NOW, cursor=1), NOW)
        await _settled(c)
        return answer

    assert asyncio.run(run()) == "답"
    assert "자기 점검을 못 했다" in caplog.text and checks.since(NOW - timedelta(days=1)) == []


def test_gaps_answers_at_once_when_nothing_was_stuck_and_later_when_it_writes(
    tmp_path: Path,
) -> None:
    from src.brain.converse import Later
    from src.storage.selfchecks import SelfCheck

    c = _build(tmp_path)
    assert _ask(c, "/gaps") == "자기 점검은 아직 안 붙었어."
    judge = Holding(reply="- 로그인이 필요한 사이트 1번 — 로그인 세션 쓰기")
    judge.release.set()
    checks = _checked(c, tmp_path, judge)
    assert _ask(c, "/gaps") == "최근 14일 동안 돌아본 답이 없어."

    def keep(result: str, blocked: str) -> None:
        checks.add(SelfCheck(NOW, "카드 내역", "a", (), result, blocked, missing="", note="막힘"))

    keep("다 함", "없음")
    assert "막힌 답은 없었어" in str(_ask(c, "/gaps")) and judge.prompts == []
    keep("못 함", "로그인이 필요함")
    later = c.reply(Incoming(text="/gaps", at=NOW, cursor=1), NOW)
    reply = asyncio.run(later)
    assert isinstance(reply, Later) and "돌아보는 중" in reply.now
    text = asyncio.run(_await(reply.then))
    assert "로그인이 필요함 1번" in text and "로그인 세션 쓰기" in text
    assert "/gaps" in str(_ask(c, "/help"))


def test_on_the_tool_path_the_answer_does_not_wait_either(tmp_path: Path) -> None:
    """운영에는 늘 도구가 있다 — 그 길(도구 고리 바로·찾아본 뒤)에서도 답이 돌아보기를 안
    기다린다(1차 리뷰: 도구 없는 길만 시험했다)."""
    from src.brain.client import ToolCall, Turn
    from src.brain.converse import Later

    call = ToolCall(id="c1", name="web_search", arguments={"query": "q"}, raw={"id": "c1"})
    c, _ = _with_tools(
        tmp_path,
        [Turn(content="바로 답"), Turn(content="", calls=(call,)), Turn(content="찾은 답")],
    )
    judge = Holding()
    checks = _checked(c, tmp_path, judge)
    _grow(tmp_path, c)

    async def run() -> tuple:
        first = await asyncio.wait_for(c.reply(Incoming(text="안녕", at=NOW, cursor=1), NOW), 2)
        second = await asyncio.wait_for(c.reply(Incoming(text="찾아 줘", at=NOW, cursor=2), NOW), 2)
        assert isinstance(second, Later)
        found = await asyncio.wait_for(second.then, 2)
        before = checks.since(NOW - timedelta(days=1))
        judge.release.set()
        await _settled(c)
        return first, found, before

    first, found, before = asyncio.run(run())
    assert (first, found, before) == ("바로 답", "찾은 답", [])
    kept = checks.since(NOW - timedelta(days=1))
    assert [k.answer for k in kept] == ["바로 답", "찾은 답"]
    # 답할 때 본 것(기록·지금 질문)을 돌아보기도 본다 — 기록으로 한 답을 "지어냄"으로 봤다(1차
    # 리뷰).
    assert "채용준비" in judge.prompts[0] and "[지금 질문]\n안녕" in judge.prompts[0]
    assert "- 내 기록으로 답하기:" in judge.prompts[0] and "- web_search:" in judge.prompts[0]


def test_brain_trouble_commands_and_buttons_are_not_looked_back_on(tmp_path: Path) -> None:
    """두뇌 장애(닿지 않음·거절)는 기능이 아니라 장애, 명령어·버튼은 생각한 답이 아니다. 한 루프
    안에서 다 돌게 둔다 — `asyncio.run` 이면 띄운 돌아보기가 시작 전에 취소돼 시험이 못 문다(1차
    리뷰)."""
    import httpx

    from src.brain.converse import DOWN, STUCK
    from src.brain.tools import ToolLoop

    @dataclass
    class Raising:
        error: Exception

        async def chat(self, messages, tools=None, thinking=None):  # type: ignore[no-untyped-def]
            raise self.error

    c = _build(tmp_path)
    judge = Holding()
    judge.release.set()
    checks = _checked(c, tmp_path, judge)

    async def run() -> List[Any]:
        said = []
        for error in (
            httpx.ConnectError("down", request=httpx.Request("POST", "http://brain")),
            RuntimeError("bad"),
        ):
            c.tools = ToolLoop(client=Raising(error), search=OneResult(), reader=None)  # type: ignore[arg-type]
            said.append(await c.reply(Incoming(text="질문", at=NOW, cursor=1), NOW))
        for text in ("/status", "/help", "/topics"):
            said.append(await c.reply(Incoming(text=text, at=NOW, cursor=1), NOW))
        said.append(await c.reply(Incoming(text="", at=NOW, cursor=1, action="fb:1:x"), NOW))
        await asyncio.sleep(0.05)
        await _settled(c)
        return said

    said = asyncio.run(run())
    assert said[:2] == [DOWN, STUCK]
    assert judge.prompts == [] and checks.since(NOW - timedelta(days=1)) == []


def test_a_done_answer_is_kept_too(tmp_path: Path) -> None:
    """다 함도 남긴다 — 안 남기면 센 것의 "다 함"이 늘 0이다."""
    c = _build(tmp_path, CountingReasoner(answer="응"))
    judge = Holding(reply='{"result": "다 함", "blocked": "없음"}')
    judge.release.set()
    checks = _checked(c, tmp_path, judge)

    async def run() -> None:
        await c.reply(Incoming(text="안녕", at=NOW, cursor=1), NOW)
        await _settled(c)

    asyncio.run(run())
    assert [k.result for k in checks.since(NOW - timedelta(days=1))] == ["다 함"]


def test_gaps_hands_the_write_up_what_exists_and_does_not_write_twice(tmp_path: Path) -> None:
    """정리도 지금 있는 기능·기록 이름을 안다(없으면 있는 기록을 "연동할 기능"으로 썼다), 연달아
    누르면 두 번 쓰지 않는다."""
    from src.brain.converse import Later
    from src.storage.selfchecks import SelfCheck

    c, _ = _with_tools(tmp_path, [])
    judge = Holding(reply="- 정리")
    judge.release.set()
    checks = _checked(c, tmp_path, judge)
    _grow(tmp_path, c)
    checks.add(SelfCheck(NOW, "카드 내역", "a", (), "못 함", "내 기록에 없음", "", "없음"))

    async def run() -> tuple:
        first = await c.reply(Incoming(text="/gaps", at=NOW, cursor=1), NOW)
        again = await c.reply(Incoming(text="/gaps", at=NOW, cursor=2), NOW)
        assert isinstance(first, Later)
        text = await first.then
        after = await c.reply(Incoming(text="/gaps", at=NOW, cursor=3), NOW)
        assert isinstance(after, Later)  # 끝나면 다시 쓸 수 있다
        await after.then
        return again, text

    again, text = asyncio.run(run())
    assert again == "이미 돌아보는 중이야. 끝나면 보낼게." and "부족한 기능" in text
    prompt = judge.prompts[0]
    assert "- web_search:" in prompt and "- 내 기록으로 답하기: 요즘 관심사(주제 나무)" in prompt
