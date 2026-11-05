"""대화 응답 — 싼 길과 비싼 길.

명령어는 LLM 을 안 불러야 한다. "지금 뭘 알고 있나"는 자비스가 생각할
필요가 없는 질문이고, 3090 을 데우는 동안 기다릴 이유도 없다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

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
    c.speech.record("topic:7", NOW, "요즘 정보처리기사 보네", token="t7")
    c.speech.record("late_sleep", NOW, "어젯밤 늦게 잤네", token="s1")

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


def test_report는_보고서를_쓰는_쪽에_맡긴다(tmp_path: Path) -> None:
    """명령어 꼴이지만 생각을 시키는 요청이다. 없으면 안 붙었다고 정직하게."""

    @dataclass
    class FakeReporter:
        calls: List[datetime] = field(default_factory=list)

        async def write(self, now: datetime) -> str:
            self.calls.append(now)
            return "# 요즘의 너"

    c = _build(tmp_path)
    assert "아직 안 붙었어" in (_ask(c, "/report") or "")
    reporter = FakeReporter()
    c.reporter = reporter  # type: ignore[assignment]
    assert _ask(c, "/report") == "# 요즘의 너" and reporter.calls == [NOW]
    assert "/report" in (_ask(c, "/help") or "")
