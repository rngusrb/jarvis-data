"""회고 엔진.

LLM 응답은 가짜로 두고 **그 주변의 규율**만 본다 — 근거는 보여준 번호로만, 이름 난장판
방지(나무를 다 보여준다), 오래 잠잠한 주제를 프롬프트 전에 접는 것, 못 읽은 응답은 시끄럽게.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence

import pytest

from src.brain.reflect import ReflectionFailed, Reflector, _parse
from src.core.models import Trace
from src.core.topics import CODE, RETIRE_AFTER, Op, plan
from src.core.traces import TraceKind
from src.storage.topics import SQLiteTopicStore
from src.storage.traces import SQLiteTraceStore

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
WEB = TraceKind(kind="web_visit", label="웹 방문", collector="mac_chrome")


@dataclass
class ScriptedReasoner:
    reply: str
    prompts: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.prompts.append(prompt)
        return self.reply


def _web(text: str, at: datetime) -> Trace:
    return Trace(source="mac_chrome", kind="web_visit", text=text, at=at)


def _build(tmp_path: Path, reply: str) -> Reflector:
    """새 DB 라 흔적 번호는 1, 2 다."""
    db = tmp_path / "t.db"
    traces = SQLiteTraceStore(db)
    traces.write(
        [
            _web(t, NOW - timedelta(hours=2 - i))
            for i, t in enumerate(["검색: 전세자금대출 금리", "검색: 버팀목 자격"])
        ]
    )
    return Reflector(
        reasoner=ScriptedReasoner(reply),
        traces=traces,
        topics=SQLiteTopicStore(db),
        kinds=[WEB],
    )


def _prompt(reflector: Reflector) -> str:
    reasoner = reflector.reasoner
    assert isinstance(reasoner, ScriptedReasoner)
    return reasoner.prompts[-1]


GOOD = """{"ops": [
  {"op": "create", "ref": "새1", "name": "주거", "evidence": [1, 2], "reason": "전세 대출"},
  {"op": "note", "topic": "새1", "text": "전세자금대출 조건을 알아보는 중"}
]}"""


def test_grows_a_topic_from_traces(tmp_path: Path) -> None:
    reflector = _build(tmp_path, GOOD)
    result = asyncio.run(reflector.run_once(NOW))

    assert result.considered == 2 and result.notes == 1
    assert result.applied == ["새 주제 #1 주거 (근거 2) — 전세 대출"]
    assert "#1 " in _prompt(reflector) and "#2 " in _prompt(reflector)
    assert reflector.topics.last_run() == NOW


def test_a_thin_topic_is_rejected_and_reported(tmp_path: Path) -> None:
    """근거를 실제보다 세게 말하는 게 이 방식의 주된 실패 모양이다."""
    thin = '{"ops": [{"op": "create", "name": "주거", "evidence": [1]}]}'
    result = asyncio.run(_build(tmp_path, thin).run_once(NOW))
    assert result.applied == []
    assert "근거가 1개뿐" in result.rejected[0]


def test_evidence_must_be_a_trace_in_the_prompt(tmp_path: Path) -> None:
    """창 밖 흔적(번호 3)은 실제로 있어도 이번에 보여주지 않았다 — 근거가 될 수 없다."""
    reflector = _build(tmp_path, '{"ops": [{"op": "create", "name": "주거", "evidence": [1, 3]}]}')
    reflector.traces.write([_web("검색: 오래된 것", NOW - timedelta(days=30))])
    result = asyncio.run(reflector.run_once(NOW))
    assert result.applied == [] and "보여주지 않은 흔적 번호 1개" in result.rejected[0]


def test_shows_the_tree_to_prevent_name_sprawl(tmp_path: Path) -> None:
    """나무를 안 보여주면 같은 걸 매번 새 이름으로 만든다."""
    reflector = _build(tmp_path, GOOD)
    asyncio.run(reflector.run_once(NOW))
    reflector.traces.write([_web("검색: 전세 계약", NOW + timedelta(hours=20))])
    asyncio.run(reflector.run_once(NOW + timedelta(days=1)))

    prompt = _prompt(reflector)
    assert "#1 주거 — 처음 보임 · 근거 2개(1일)" in prompt
    assert "메모 08-27: 전세자금대출 조건을 알아보는 중" in prompt
    assert "근거 #2 08-27 20:00 검색: 버팀목 자격" in prompt


def test_attached_traces_are_not_offered_again(tmp_path: Path) -> None:
    """주제에 붙은 흔적은 나무 쪽(근거 예)에만 나온다. 매일 다시 보여주면 토큰만 먹는다."""
    reflector = _build(tmp_path, GOOD)
    asyncio.run(reflector.run_once(NOW))
    reflector.traces.write([_web("검색: 전세 계약", NOW + timedelta(hours=20))])
    result = asyncio.run(reflector.run_once(NOW + timedelta(days=1)))

    assert result.considered == 1
    assert "[웹 방문 — 아직 주제에 안 붙은 것] 1건" in _prompt(reflector)


def test_does_not_see_traces_after_now(tmp_path: Path) -> None:
    """되감기가 그날 아침에 아직 없던 흔적을 보면 안 된다."""
    reflector = _build(tmp_path, '{"ops": []}')
    reflector.traces.write([_web("검색: 내일 것", NOW + timedelta(hours=1))])
    result = asyncio.run(reflector.run_once(NOW))
    assert result.considered == 2 and "내일 것" not in _prompt(reflector)


def test_does_not_read_kinds_kept_out_of_reflection(tmp_path: Path) -> None:
    """앱 사용 구간은 하루 수백 줄이다. 낱줄로 넣으면 관심사가 아니라 잡음이 된다."""
    reflector = _build(tmp_path, GOOD)
    reflector.traces.write(
        [Trace(source="mac_apps", kind="mac_app", text="Cursor 42분", at=NOW - timedelta(hours=1))]
    )
    app = TraceKind(kind="mac_app", label="맥 앱 사용", collector="mac_apps", reflect=False)
    reflector.kinds = [WEB, app]

    asyncio.run(reflector.run_once(NOW))

    assert "전세자금대출" in _prompt(reflector)
    assert "Cursor" not in _prompt(reflector)


def test_folds_kinds_kept_out_of_reflection_into_day_digests(tmp_path: Path) -> None:
    """앱 사용은 낱줄 대신 하루 요약으로 들어간다. 요약은 창 안의 흔적을 상한 없이 전부 본다."""
    reflector = _build(tmp_path, GOOD)
    reflector.per_kind_limit = 1
    reflector.traces.write(
        [
            Trace(
                source="mac_apps",
                kind="mac_app",
                text=f"Cursor {i}분",
                at=NOW - timedelta(hours=i + 1),
            )
            for i in range(3)
        ]
    )
    seen: List[tuple] = []

    def digest(traces: Sequence[Trace], since: datetime, until: datetime) -> List[str]:
        seen.append((len(traces), since, until))
        return ["08-26(수) 09:00~23:00, 모두 3시간 — Cursor 3시간"]

    app = TraceKind(
        kind="mac_app", label="맥 앱 사용", collector="mac_apps", reflect=False, digest=digest
    )
    reflector.kinds = [WEB, app]

    result = asyncio.run(reflector.run_once(NOW))

    assert "[맥 앱 사용 — 하루 요약] 1일" in _prompt(reflector)
    assert "Cursor 3시간" in _prompt(reflector)
    assert "Cursor 0분" not in _prompt(reflector)
    # 상한(1)에 잘리면 요약이 아무 표시 없이 줄어든다.
    assert seen == [(3, NOW - timedelta(days=7), NOW)]
    assert result.digested == 1
    # 낱줄 상한은 최신 것부터 채운다.
    assert result.considered == 1 and "버팀목" in _prompt(reflector)


def test_kinds_read_line_by_line_ignore_their_digest(tmp_path: Path) -> None:
    """웹 방문은 요약이 있어도 회고는 낱줄로 훑는다 — 요약은 제목과 검색어 대부분을 버린다."""
    reflector = _build(tmp_path, GOOD)
    called: List[int] = []

    def digest(traces: Sequence[Trace], since: datetime, until: datetime) -> List[str]:
        called.append(len(traces))
        return ["요약 한 줄"]

    reflector.kinds = [
        TraceKind(kind="web_visit", label="웹 방문", collector="mac_chrome", digest=digest)
    ]
    asyncio.run(reflector.run_once(NOW))

    assert "전세자금대출" in _prompt(reflector) and "요약 한 줄" not in _prompt(reflector)
    assert called == []


def test_reflects_on_digests_alone(tmp_path: Path) -> None:
    """웹 기록이 없는 주에도 앱 사용 요약만으로 회고가 돈다."""
    db = tmp_path / "t.db"
    traces = SQLiteTraceStore(db)
    traces.write([Trace(source="mac_apps", kind="mac_app", text="Cursor 42분", at=NOW)])
    app = TraceKind(
        kind="mac_app",
        label="맥 앱 사용",
        collector="mac_apps",
        reflect=False,
        digest=lambda found, since, until: [f"{len(found)}줄"],
    )
    reasoner = ScriptedReasoner('{"ops": []}')
    reflector = Reflector(
        reasoner=reasoner, traces=traces, topics=SQLiteTopicStore(db), kinds=[WEB, app]
    )

    result = asyncio.run(reflector.run_once(NOW + timedelta(minutes=1)))

    assert len(reasoner.prompts) == 1 and "1줄" in reasoner.prompts[0]
    assert result.considered == 0 and result.digested == 1


def test_nothing_to_read_is_still_a_run(tmp_path: Path) -> None:
    """훑을 게 없는 날도 "돌았다" — 매일 루프가 그날 다시 돌지 않게."""
    db = tmp_path / "t.db"
    reasoner = ScriptedReasoner('{"ops": []}')
    reflector = Reflector(
        reasoner=reasoner, traces=SQLiteTraceStore(db), topics=SQLiteTopicStore(db), kinds=[WEB]
    )
    asyncio.run(reflector.run_once(NOW))
    assert reasoner.prompts == [] and reflector.topics.last_run() == NOW


def test_shows_times_in_korean_time(tmp_path: Path) -> None:
    """UTC 그대로 보여주면 낮 방문이 새벽 방문으로 읽힌다."""
    reflector = _build(tmp_path, GOOD)
    reflector.traces.write(
        [_web("점심에 본 페이지", datetime(2026, 8, 27, 3, 56, tzinfo=timezone.utc))]
    )

    asyncio.run(reflector.run_once(datetime(2026, 8, 27, 15, 30, tzinfo=timezone.utc)))

    assert "08-27 12:56  점심에 본 페이지" in _prompt(reflector)
    # UTC 로는 아직 27일이지만 한국은 28일 새벽이다.
    assert "지금: 2026-08-28(금) 00:30" in _prompt(reflector)


def test_retires_long_quiet_topics_before_it_prompts(tmp_path: Path) -> None:
    """오래 조용한 주제를 그대로 보여주면 모델이 거기에 아무거나 붙여 되살린다."""
    reflector = _build(tmp_path, '{"ops": []}')
    reflector.topics.apply(
        plan(
            reflector.topics.load(), [Op("create", name="옛 관심사", evidence=(1, 2))], {1, 2}, NOW
        ),
        NOW,
    )
    later = NOW + RETIRE_AFTER + timedelta(days=1)
    reflector.traces.write([_web("검색: 요즘 것", later - timedelta(hours=1))])

    result = asyncio.run(reflector.run_once(later))

    assert result.retired == [
        f"#1 옛 관심사 접음 — {RETIRE_AFTER.days + 1}일 동안 근거가 없다(마지막 08-27)"
    ]
    assert "옛 관심사" not in _prompt(reflector)
    # 코드가 접은 것은 모델의 구조 변경으로 세지 않는다.
    assert [c.actor for c in reflector.topics.changes() if c.op == "retire"] == [CODE]


def test_quiet_topics_are_listed_so_a_return_is_not_a_new_topic(tmp_path: Path) -> None:
    reflector = _build(tmp_path, '{"ops": []}')
    reflector.topics.apply(
        plan(reflector.topics.load(), [Op("create", name="야구", evidence=(1, 2))], {1, 2}, NOW),
        NOW,
    )
    later = NOW + timedelta(days=40)
    reflector.traces.write([_web("검색: 한국시리즈", later - timedelta(hours=1))])

    asyncio.run(reflector.run_once(later))

    assert "[잠잠한 주제]" in _prompt(reflector) and "#1 야구 (마지막 08-27)" in _prompt(reflector)


def test_an_unreadable_reply_fails_loudly_and_is_recorded(tmp_path: Path) -> None:
    """못 읽은 응답을 "바꿀 것 없음"으로 치면 그날 회고가 조용히 사라진다."""
    reflector = _build(tmp_path, "생각해보니 잘 모르겠다")
    with pytest.raises(ReflectionFailed):
        asyncio.run(reflector.run_once(NOW))
    assert reflector.topics.last_run() is None
    assert _outcomes(tmp_path) == [
        "failed: 회고 응답을 못 읽었다 — 모델의 연산은 하나도 적용하지 않았다"
    ]


def _outcomes(tmp_path: Path) -> List[str]:
    conn = sqlite3.connect(tmp_path / "t.db")
    try:
        return [r[0] for r in conn.execute("SELECT outcome FROM reflections ORDER BY id")]
    finally:
        conn.close()


def test_traces_of_a_retired_topic_are_not_offered_again(tmp_path: Path) -> None:
    """접힌 주제의 흔적을 다시 내밀면 모델이 같은 이름으로 또 만든다(리뷰 재현)."""
    reflector = _build(tmp_path, '{"ops": []}')
    tree = reflector.topics.load()
    reflector.topics.apply(
        plan(tree, [Op("create", name="삿포로", evidence=(1, 2))], {1, 2}, NOW), NOW
    )
    topic = reflector.topics.load().live()[0].id
    reflector.topics.apply(
        plan(reflector.topics.load(), [Op("retire", topic=topic, reason="다녀왔다")], set(), NOW),
        NOW,
    )
    reflector.traces.write([_web("검색: 새 것", NOW + timedelta(hours=1))])

    result = asyncio.run(reflector.run_once(NOW + timedelta(hours=2)))

    assert result.considered == 1 and "전세자금대출" not in _prompt(reflector)


def test_dropped_evidence_numbers_are_logged_and_counted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """근거를 세게 말하는 게 주된 실패 모양이다. 반환값에만 두면 서버에서는 안 보인다(리뷰 재현)."""
    reply = '{"ops": [{"op": "create", "name": "주거", "evidence": [1, 2, 901, 902]}]}'
    reflector = _build(tmp_path, reply)
    with caplog.at_level(logging.INFO, logger="src.brain.reflect"):
        asyncio.run(reflector.run_once(NOW))
    assert "보여주지 않은 흔적 번호 2개는 뺐다" in caplog.text
    conn = sqlite3.connect(tmp_path / "t.db")
    try:
        assert conn.execute("SELECT changed, notices FROM reflections").fetchall() == [(1, 1)]
    finally:
        conn.close()


def test_the_prompt_budget_drops_the_oldest_lines_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """나무와 메모는 상한 없이 자란다.

    흔적 줄이 남은 몫을 최신 것부터 채우고, 뺀 건 경고로 남긴다.
    """
    reflector = _build(tmp_path, '{"ops": [{"op": "create", "name": "주거", "evidence": [1, 2]}]}')
    reflector.traces.write(
        [_web(f"검색: 최근 {i}", NOW - timedelta(minutes=50 - i)) for i in range(10)]
    )
    reflector.budget_chars = 300
    with caplog.at_level(logging.WARNING, logger="src.brain.reflect"):
        result = asyncio.run(reflector.run_once(NOW))

    prompt = _prompt(reflector)
    assert 0 < result.considered < 12 and result.trimmed == 12 - result.considered
    assert "검색: 최근 9" in prompt and "전세자금대출" not in prompt
    assert f"오래된 {result.trimmed}건은 뺐다" in prompt and "프롬프트 상한" in caplog.text
    # 보여주지 않은 줄은 근거가 될 수 없다.
    assert result.applied == [] and "보여주지 않은 흔적 번호 2개" in result.rejected[0]


def test_parse_tolerates_fences_and_chatter() -> None:
    """완벽한 형식을 요구하는 것보다 관대하게 읽는 쪽이 실제로 잘 돈다."""
    messy = (
        '알겠습니다.\n```json\n{"ops": [{"op": "attach", "topic": "#3", "evidence": ["12", 13]}]}'
        "\n```"
    )
    assert _parse(messy) == [Op("attach", topic=3, evidence=(12, 13))]
    assert _parse('[{"op": "note", "topic": null, "text": "a"}]') == [Op("note", text="a")]
    assert _parse('{"ops": []}') == []
    # ref 를 '#5'·'05' 로 지어도 가리키는 쪽(5)과 같은 이름이 된다 — 있는 #5 로 새지 않게.
    assert [
        op.ref for op in _parse('{"ops": [{"op": "create", "ref": "#5"}, {"ref": "05"}]}') or []
    ] == [
        "5",
        "5",
    ]
    assert _parse("설명만 하고 JSON 이 없음") is None
    assert _parse("") is None
