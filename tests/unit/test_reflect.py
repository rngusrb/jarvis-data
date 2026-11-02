"""회고 엔진.

LLM 응답은 가짜로 두고 **그 주변의 규율**만 본다 — 근거 강제, 이름 난장판
방지, 시든 믿음을 프롬프트에서 빼는 것.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from src.brain.reflect import Reflector, _parse
from src.core.beliefs import FORGET_AFTER, Belief, Status
from src.core.models import Trace
from src.core.traces import TraceKind
from src.storage.beliefs import SQLiteBeliefStore
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


def _build(tmp_path: Path, reply: str) -> Reflector:
    db = tmp_path / "t.db"
    traces = SQLiteTraceStore(db)
    traces.write(
        [
            Trace(source="mac_chrome", kind="web_visit", text=t, at=NOW - timedelta(hours=i))
            for i, t in enumerate(["검색: 전세자금대출 금리", "검색: 버팀목 자격"])
        ]
    )
    return Reflector(
        reasoner=ScriptedReasoner(reply),
        traces=traces,
        beliefs=SQLiteBeliefStore(db),
        kinds=[WEB],
    )


GOOD = """[
  {"kind": "관심사:주거", "value": "전세자금대출을 알아보는 중",
   "confidence": 0.8,
   "evidence": ["검색: 전세자금대출 금리", "검색: 버팀목 자격"]}
]"""


def test_learns_a_belief_from_traces(tmp_path: Path) -> None:
    result = asyncio.run(_build(tmp_path, GOOD).run_once(NOW))
    assert result.considered == 2
    assert [b.kind for b in result.learned] == ["관심사:주거"]


def test_rejects_a_belief_without_enough_evidence(tmp_path: Path) -> None:
    """근거를 실제보다 세게 말하는 게 이 방식의 주된 실패 모양이다."""
    thin = '[{"kind": "관심사:주거", "value": "이사 준비 중", "confidence": 0.9, "evidence": []}]'
    result = asyncio.run(_build(tmp_path, thin).run_once(NOW))
    assert result.learned == []
    assert "근거가 0개" in result.skipped[0]


def test_shows_existing_kinds_to_prevent_name_sprawl(tmp_path: Path) -> None:
    """기존 목록을 안 보여주면 같은 걸 매번 새 이름으로 만든다."""
    reflector = _build(tmp_path, GOOD)
    asyncio.run(reflector.run_once(NOW))
    asyncio.run(reflector.run_once(NOW + timedelta(days=1)))

    reasoner = reflector.reasoner
    assert isinstance(reasoner, ScriptedReasoner)
    assert "관심사:주거" in reasoner.prompts[-1]
    assert "위 kind를 그대로 써라" in reasoner.prompts[-1]


def test_does_not_read_kinds_kept_out_of_reflection(tmp_path: Path) -> None:
    """앱 사용 구간은 하루 수백 줄이다. 낱줄로 넣으면 관심사가 아니라 잡음이 된다."""
    reflector = _build(tmp_path, GOOD)
    reflector.traces.write(
        [Trace(source="mac_apps", kind="mac_app", text="Cursor 42분", at=NOW - timedelta(hours=1))]
    )
    app = TraceKind(kind="mac_app", label="맥 앱 사용", collector="mac_apps", reflect=False)
    reflector.kinds = [WEB, app]

    asyncio.run(reflector.run_once(NOW))

    reasoner = reflector.reasoner
    assert isinstance(reasoner, ScriptedReasoner)
    assert "전세자금대출" in reasoner.prompts[-1]
    assert "Cursor" not in reasoner.prompts[-1]


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

    reasoner = reflector.reasoner
    assert isinstance(reasoner, ScriptedReasoner)
    assert "[맥 앱 사용 — 하루 요약] 1일" in reasoner.prompts[-1]
    assert "Cursor 3시간" in reasoner.prompts[-1]
    assert "Cursor 0분" not in reasoner.prompts[-1]
    # 상한(1)에 잘리면 요약이 아무 표시 없이 줄어든다.
    assert seen == [(3, NOW - timedelta(days=7), NOW)]
    assert result.digested == 1


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

    reasoner = reflector.reasoner
    assert isinstance(reasoner, ScriptedReasoner)
    assert "전세자금대출" in reasoner.prompts[-1] and "요약 한 줄" not in reasoner.prompts[-1]
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
    reasoner = ScriptedReasoner("[]")
    reflector = Reflector(
        reasoner=reasoner, traces=traces, beliefs=SQLiteBeliefStore(db), kinds=[WEB, app]
    )

    result = asyncio.run(reflector.run_once(NOW + timedelta(minutes=1)))

    assert len(reasoner.prompts) == 1 and "1줄" in reasoner.prompts[0]
    assert result.considered == 0 and result.digested == 1


def test_shows_times_in_korean_time(tmp_path: Path) -> None:
    """UTC 그대로 보여주면 낮 방문이 새벽 방문으로 읽힌다."""
    reflector = _build(tmp_path, GOOD)
    reflector.traces.write(
        [
            Trace(
                source="mac_chrome",
                kind="web_visit",
                text="점심에 본 페이지",
                at=datetime(2026, 8, 27, 3, 56, tzinfo=timezone.utc),
            )
        ]
    )

    asyncio.run(reflector.run_once(datetime(2026, 8, 27, 15, 30, tzinfo=timezone.utc)))

    reasoner = reflector.reasoner
    assert isinstance(reasoner, ScriptedReasoner)
    assert "08-27 12:56  점심에 본 페이지" in reasoner.prompts[-1]
    # UTC 로는 아직 27일이지만 한국은 28일 새벽이다.
    assert "오늘은 2026-08-28 이다" in reasoner.prompts[-1]


def test_forgets_before_it_prompts(tmp_path: Path) -> None:
    """시든 믿음이 프롬프트에 남으면 모델이 그걸 근거로 스스로 되살린다."""
    reflector = _build(tmp_path, "[]")
    reflector.beliefs.observe(
        Belief(
            kind="관심사:죽은것",
            value="옛날 관심사",
            confidence=0.9,
            first_seen=NOW,
            last_seen=NOW,
            evidence=("옛날 검색",),
        ),
        NOW,
    )
    much_later = NOW + FORGET_AFTER + timedelta(days=1)
    # 그때도 볼 흔적이 있어야 프롬프트가 만들어진다. 흔적이 없으면 회고는
    # 모델을 부르지도 않는데, 그건 이 테스트가 보려는 게 아니다.
    reflector.traces.write(
        [Trace(source="mac_chrome", kind="web_visit", text="검색: 요즘 것", at=much_later)]
    )
    result = asyncio.run(reflector.run_once(much_later))

    assert result.forgotten == ["관심사:죽은것"]
    reasoner = reflector.reasoner
    assert isinstance(reasoner, ScriptedReasoner)
    assert "관심사:죽은것" not in reasoner.prompts[-1]


def test_parse_tolerates_fences_and_chatter() -> None:
    """완벽한 형식을 요구하는 것보다 관대하게 읽는 쪽이 실제로 잘 돈다."""
    messy = '알겠습니다.\n```json\n[{"kind": "a", "value": "b"}]\n```\n이상입니다.'
    assert _parse(messy) == [{"kind": "a", "value": "b"}]
    assert _parse("설명만 하고 배열이 없음") == []
    assert _parse("") == []


SPLIT = """[
  {"kind": "관심사:개발", "value": "RAG 공부",
   "evidence": ["검색: bm25 vs bge m3", "임베딩 방식 - Claude"]},
  {"kind": "관심사:개발", "value": "코딩 도구 비교",
   "evidence": ["Claude Code 문서"]}
]"""


def test_same_kind_in_one_reply_is_merged(tmp_path: Path) -> None:
    """실제 사고의 재현.

    모델이 "관심사:개발"을 네 조각으로 내놨다. 저장소는 kind가 기본키라
    서로 덮어썼고, 값은 마지막 것이 남고 근거는 전부 합쳐져 **짝이
    어긋났다** — "코딩 어시스턴트 비교"에 농수산물 가격이 근거로 붙었다.
    """
    result = asyncio.run(_build(tmp_path, SPLIT).run_once(NOW))

    assert len(result.learned) == 1
    learned = result.learned[0]
    # 값은 근거가 두꺼운 쪽을 따른다.
    assert learned.value == "RAG 공부"
    assert learned.evidence == ("검색: bm25 vs bge m3", "임베딩 방식 - Claude", "Claude Code 문서")


def test_one_run_cannot_confirm_itself(tmp_path: Path) -> None:
    """후보→확정은 "나중에 또 나왔다"는 뜻이어야 한다."""
    result = asyncio.run(_build(tmp_path, SPLIT).run_once(NOW))
    assert result.learned[0].status is Status.CANDIDATE


def test_repeated_evidence_counts_once(tmp_path: Path) -> None:
    """같은 검색을 네 번 했다고 근거가 네 개가 되면 확신이 부풀려진다.

    반복 자체는 신호지만(못 알아냈다는 뜻이다) 그건 value 가 말할 일이다.
    """
    repeated = """[{"kind": "관심사:일상", "value": "애플워치 워키토키를 못 알아냄",
      "evidence": ["검색: 애플워치 워키토키 사용법", "검색: 애플워치 워키토키 사용법",
                   "검색: 애플워치 워키토키 사용법", "검색: 애플워치 워키토키 켜기"]}]"""
    result = asyncio.run(_build(tmp_path, repeated).run_once(NOW))
    assert result.learned[0].evidence == (
        "검색: 애플워치 워키토키 사용법",
        "검색: 애플워치 워키토키 켜기",
    )
