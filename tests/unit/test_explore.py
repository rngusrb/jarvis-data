"""무작위 탐색 — 씨앗 고르기, 찾기, 판단의 꼴, 날짜, 남기기(JAR-046)."""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, cast

import httpx
import pytest

from src.brain.explore import (
    BROKEN,
    FACTS,
    FREE,
    INTEREST,
    JUDGED,
    RELATIVE_WORDS,
    RETRY,
    TIMEOUT,
    UNSEARCHED,
    Explorer,
    SearchDown,
    Seed,
    clean,
    date_table,
    parse_judged,
    render_table,
)
from src.brain.tools import Researched, Use
from src.brain.web import Page
from src.core.clock import stamp
from src.core.findings import KEEP, NOTHING, TELL, Fact, Finding, when_label
from src.core.models import Trace
from src.core.topics import Op, plan
from src.storage.findings import SQLiteFindings
from src.storage.selfchecks import SelfCheck
from src.storage.topics import SQLiteTopicStore
from src.storage.traces import SQLiteTraceStore

NOW = datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc)  # 한국 11/12(목) 14:00
TODAY = date(2026, 11, 12)
EVIDENCE = "가나은행 하반기 서류 마감 2026.11.16(월) 18:00 — 가나은행 채용"


def _found(
    text: str = EVIDENCE, searched: bool = True, material: Tuple[str, ...] = (EVIDENCE,)
) -> Researched:
    uses = (Use("search", "가나은행 채용 일정", ok=True, note="", count=5 if searched else 0),)
    page = Page(url="https://example.com/recruit", title="채용", text=EVIDENCE, cut=False)
    return Researched(
        text=text,
        uses=uses,
        pages=(page,) if searched else (),
        material=material if searched else (),
    )


@dataclass
class FakeTools:
    result: Researched = field(default_factory=_found)
    delay: float = 0.0
    fail: Optional[BaseException] = None
    browse: Any = None
    asked: List[Any] = field(default_factory=list)
    guide: str = "(도구 안내)"

    async def run(self, messages: List[Any]) -> Researched:
        self.asked.append(messages)
        if self.fail is not None:
            raise self.fail
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


@dataclass
class FakeJudge:
    answer: str
    fail: Optional[BaseException] = None
    prompts: List[str] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.prompts.append(prompt)
        if self.fail is not None:
            raise self.fail
        return self.answer


def _json(worth: str = TELL, facts: Optional[list] = None, title: str = "하반기 서류 마감") -> str:
    if facts is None:
        facts = [{"text": "가나은행 하반기 서류 마감", "when": 1, "source": "가나은행 채용"}]
    return json.dumps(
        {"worth": worth, "title": title, "facts": facts, "idea": "채용 마감 달력"},
        ensure_ascii=False,
    )


@dataclass
class Checks:
    checks: Sequence[SelfCheck]

    def since(self, at: datetime) -> List[SelfCheck]:
        return [c for c in self.checks if c.at >= at]


def _check(
    question: str,
    blocked: str = "검색 결과가 모자람",
    hours_ago: float = 3,
    result: str = "일부",
    missing: str = "",
) -> SelfCheck:
    return SelfCheck(
        at=NOW - timedelta(hours=hours_ago),
        question=question,
        answer="-",
        tools=(),
        result=result,
        blocked=blocked,
        missing=missing,
        note="-",
    )


def _explorer(
    tmp_path: Path,
    judge: str = "",
    tools: Optional[FakeTools] = None,
    checks: Sequence[SelfCheck] = (),
    seed: int = 1,
) -> Explorer:
    return Explorer(
        reasoner=FakeJudge(judge or _json()),
        tools=cast(Any, tools or FakeTools()),
        findings=SQLiteFindings(tmp_path / "t.db"),
        checks=Checks(checks),
        rng=random.Random(seed),
    )


def _finding(key: str, at: datetime, kind: str = RETRY, note: str = "") -> Finding:
    return Finding(at, kind, key, "s", "", (), (), ("q",), KEEP, note=note)


# ── 날짜 ──────────────────────────────────────────────────────


def test_dates_come_only_from_the_codes_table_never_from_the_model() -> None:
    """2차 리뷰(재현 3/3): 기사 입력일이 "11월 중순" 면접의 날로, 작년 날짜가 올해로,
    찾기 모델이 바꿔 쓴 날짜("내일 마감" → 11/13)가 그대로 남았다. 날짜는 원자료에서
    코드가 뽑고 모델은 번호만 고른다."""
    material = (
        "가나은행 하반기 채용 — 입력 2026.11.12 09:30\n면접은 11월 중순에 진행될 예정이다.",
        "정보처리기사 실기 2025. 11. 16.(일) 시행",
        "2026.11.05 작성 … 내일 마감",
        EVIDENCE,
    )
    choices = date_table(material, TODAY)
    assert [(d.day, d.line, d.sure) for d in choices] == [(date(2026, 11, 16), EVIDENCE, True)]
    assert f'1. 2026-11-16(월) — "{EVIDENCE}"' in render_table(choices)
    assert render_table([]).startswith("[날짜표] 없음")
    raw = _json(
        facts=[
            {"text": "가나은행 하반기 면접", "when": None},
            {"text": "가나은행 하반기 서류 마감", "when": 1, "source": "가나은행 채용"},
            {"text": "번호가 틀린 것", "when": 7},
        ]
    )
    judged = parse_judged(raw, TODAY, choices)
    assert [(f.text, f.day, f.quote) for f in judged.facts] == [
        ("가나은행 하반기 면접", None, ""),
        ("가나은행 하반기 서류 마감", date(2026, 11, 16), EVIDENCE),
        ("번호가 틀린 것", None, ""),
    ]


def test_date_words_in_a_source_line_are_blanked_not_rewritten() -> None:
    """서버 실측: 원문 줄에 "D-8"이 섞였다 — 사실 옆에 7일 동안 보이니 다른 날 틀린다."""
    choices = date_table(("가나 공모전 접수 2026. 11. 19, 21:59까지D-8 · 오늘 마감 아님",), TODAY)
    assert [(d.day, d.line) for d in choices] == [
        (date(2026, 11, 19), "가나 공모전 접수 2026. 11. 19, 21:59까지… · … 마감 아님")
    ]


def test_todays_date_needs_a_task_word_beside_it() -> None:
    """3차 리뷰: 바이라인·머리의 날짜·글 쓴 시각 줄이 "오늘" 후보 1번으로 들어갔다. 글을 쓴 날은
    오늘이나 지난 날뿐이라, 오늘 날짜는 같은 줄에 일의 말이 있을 때만 받는다."""
    material = (
        "2026. 11. 12. 9:30",
        "2026년 11월 12일 목요일",
        "가나일보 2026-11-12 09:30 수정 2026-11-12 10:00",
        "서류 마감 2026.11.12(목) 18:00",
    )
    assert [d.line for d in date_table(material, TODAY)] == ["서류 마감 2026.11.12(목) 18:00"]


def test_a_day_without_a_sure_year_keeps_only_its_source_line() -> None:
    """사용자 결정(4차 리뷰 BLOCKED 뒤): 해가 확실할 때만 날짜로. 아니면 날짜 없이 원문만 — 코드가
    "내일" 같은 거리를 세지 않는다."""
    choices = date_table(("가나은행 서류 마감 11월 16일",), TODAY)
    assert [(d.day, d.sure) for d in choices] == [(date(2026, 11, 16), False)]
    assert '1. 11/16 [해 모름] — "가나은행 서류 마감 11월 16일"' in render_table(choices)
    raw = _json(facts=[{"text": "가나은행 서류 마감 11월 16일", "when": 1}])
    [fact] = parse_judged(raw, TODAY, choices).facts
    assert (fact.day, fact.no_year, fact.quote) == (None, True, "가나은행 서류 마감 11월 16일")
    assert when_label(fact, TODAY) == '해 모름 — 원문 "가나은행 서류 마감 11월 16일"'


def test_bare_days_and_weekdays_in_a_fact_must_be_its_own() -> None:
    """4차 리뷰: 날짜 거르기가 못 읽는 "16일까지"·"월요일 마감"이 사실 글에 남았다."""
    choices = date_table((EVIDENCE,), TODAY)  # 1. 2026-11-16(월)
    raw = _json(
        facts=[
            {"text": "서류는 16일까지", "when": 1},  # 제 날
            {"text": "서류는 16일(월) 마감", "when": 1},
            {"text": "면접은 20일까지", "when": 1},  # 다른 날
            {"text": "월요일 마감", "when": None},  # 날짜 없는 사실의 요일
            {"text": "매주 수요일 상시 모집", "when": None},  # 되풀이는 날짜 말이 아니다
            {"text": "가나카드 반값(1/2) 할인", "when": None},  # 분수
        ]
    )
    assert [f.text for f in parse_judged(raw, TODAY, choices).facts] == [
        "서류는 16일까지",
        "서류는 16일(월) 마감",
        "매주 수요일 상시 모집",
        "가나카드 반값(1/2) 할인",
    ]


def test_a_fact_carries_no_date_but_its_own() -> None:
    """사용자 안: 날짜는 메타, 본문은 날짜 없이. 3차 리뷰: 본문의 날짜를 "자료 어딘가에 있나"로 받아
    서류 마감 날이 "면접 11월 16일"로 남았다 — 본문의 날짜는 그 사실이 고른 날일 때만 남는다."""
    choices = date_table(("서류 마감 2026.11.16(월)\n면접은 11월 중순",), TODAY)
    raw = _json(
        facts=[
            {"text": "가나은행 면접 11월 16일", "when": None},  # 다른 일의 날
            {"text": "마감 11월 13일", "when": None},  # 자료에 없는 날
            {"text": "서류 마감 11월 16일", "when": 1},  # 제 날짜
            {"text": "서류 마감 11월 17일", "when": 1},  # 고른 날과 다르다
            {"text": "가나은행 하반기 채용 진행", "when": None},
        ],
        title="11월 16일 마감 소식",
    )
    judged = parse_judged(raw, TODAY, choices)
    assert [(f.text, f.day) for f in judged.facts] == [
        ("서류 마감 11월 16일", date(2026, 11, 16)),
        ("가나은행 하반기 채용 진행", None),
    ]
    assert judged.title == ""
    stray_idea = json.dumps(
        {"worth": KEEP, "title": "t", "facts": [], "idea": "11월 13일에 알려 주기"},
        ensure_ascii=False,
    )
    assert parse_judged(stray_idea, TODAY, choices).idea == ""


@pytest.mark.parametrize(
    "text",
    [
        "내일 마감",
        "오늘까지 접수",
        "이번 토요일까지",
        "오는 금요일 발표",
        "이달 말 발표",
        "사흘 뒤 시작",
        "일주일 남았다",
        "3일 뒤 발표",
        "내주에 공고",
        "D-3",
        "다음 주 월요일",
        # 2차 리뷰의 미탐
        "내달 1일부터",
        "금주 마감",
        "차주 공고 예정",
        "금일 마감",
        "오는 16일까지",
        "3일 전 공고",
        "2주 뒤 발표",
        "한 달 후 개강",
        # 3차 리뷰의 미탐
        "익일 발송",
        "명일 마감",
        "곧 마감",
        "마감 임박",
        "3시간 뒤 시작",
        "30분 후 오픈",
        "주말까지 신청",
        "월말까지 접수",
        "사흘 내 발표",
        # 4차 리뷰의 미탐
        "1주일 뒤 발표",
        "이튿날 발표",
    ],
)
def test_relative_date_words_are_caught(text: str) -> None:
    assert RELATIVE_WORDS.search(text), text


@pytest.mark.parametrize(
    "text",
    [
        "가나내일배움센터 지원",
        "내일배움카드 신청",
        "다모레상회 채용",
        "오늘의집 할인",
        "11월 20일 전까지 접수",
        "30일 남짓 걸린다",
        "2026 하반기 신입 공채",
        "7경기 일정",
        # 2차 리뷰의 오탐 — 달이 앞에 있으면 띄어 써도 절대 날짜
        "11월 20일 후 개별 통보",
        "12월 1일 남은 좌석",
        # 같은 글자의 다른 말
        "금주 프로그램 참가",
        "차주 보험 가입",
        "말이 내달렸다",
        "명일동 맛집",
        "곧바로 신청",
        # 4차 리뷰의 오탐 — 다른 일에 기댄 기간·사업 이름(혜택 사실이 버려졌다)
        "결제 후 30일 이내 캐시백",
        "구매 후 7일 이내 환불",
        "48시간 전 취소 시 전액 환불",
        "6개월 후 재응시 가능",
        "내일채움공제 가입",
        "내일저축계좌 신청",
    ],
)
def test_names_and_absolute_spans_are_not_date_words(text: str) -> None:
    """1·2차 리뷰의 오탐 — 이 말이 든 사실이 통째로 사라졌다."""
    assert not RELATIVE_WORDS.search(text), text


def test_facts_with_date_words_are_dropped_and_the_title_is_emptied() -> None:
    choices = date_table((EVIDENCE,), TODAY)
    raw = _json(
        facts=[
            {"text": "내일 서류 마감", "when": 1},
            {"text": "가나은행 하반기 서류 마감", "when": 1},
        ],
        title="내일이 마감이야",
    )
    judged = parse_judged(raw, TODAY, choices)
    assert [f.text for f in judged.facts] == ["가나은행 하반기 서류 마감"]
    assert judged.title == "" and judged.worth == TELL
    only_dated = _json(facts=[{"text": "내일 마감", "when": None}])
    assert parse_judged(only_dated, TODAY, choices).worth == KEEP  # 남은 사실이 없다


def test_broken_judgments_say_why() -> None:
    for broken in ("그냥 글", '{"worth": "아마도"}', "{깨진"):
        assert parse_judged(broken, TODAY).note == BROKEN
    # 꼴 없이 부른 두뇌의 이상한 칸 — 터지지 않고 날짜 없이(3차 리뷰)
    odd = '{"worth": "기록만", "title": "t", "facts": 5, "idea": ""}'
    assert parse_judged(odd, TODAY).facts == ()
    texty = _json(worth=KEEP, facts=[{"text": "가나은행 채용", "when": "1"}])
    assert [f.day for f in parse_judged(texty, TODAY, date_table((EVIDENCE,), TODAY)).facts] == [
        None
    ]


def test_facts_stop_at_five_even_from_a_brain_without_the_shape() -> None:
    """꼴(`JUDGED`)이 사실을 다섯까지로 막지만, 꼴 없이 부른 두뇌가 늘어놓아도 앞 다섯만."""
    many = [{"text": f"사실 {i}", "when": None, "source": "s"} for i in range(9)]
    judged = parse_judged(_json(facts=many), TODAY)
    assert [f.text for f in judged.facts] == [f"사실 {i}" for i in range(FACTS)]
    assert JUDGED["properties"]["facts"]["maxItems"] == FACTS
    assert JUDGED["properties"]["worth"]["enum"] == [TELL, KEEP, NOTHING]


# ── 씨앗 ──────────────────────────────────────────────────────


def test_retry_seeds_carry_when_they_were_asked_and_skip_stale_dated_questions(
    tmp_path: Path,
) -> None:
    """1차 리뷰: 11/12 22:00 의 "내일 오전 접수"를 11/13 에 다시 찾아 하루 밀린 날을 알렸다."""
    checks = [
        _check("자격증 실기 접수 일정", hours_ago=3),
        _check("내일 오전 접수 몇 시부터야", hours_ago=20),  # 어제 물은 날짜 말 질문
        _check("내 지난주 수면", blocked="내 기록에 없음", hours_ago=5),  # 웹으로 안 풀린다
        _check("방금 물은 것", hours_ago=0.5),  # 아직 한 시간 안
        _check("잘 답한 것", blocked="없음", result="다 함", hours_ago=8),
    ]
    explorer = _explorer(tmp_path, checks=checks)
    seeds = explorer.seeds(NOW)
    assert [(s.kind, s.subject) for s in seeds] == [(RETRY, "자격증 실기 접수 일정")]
    assert "물어본 때: 2026-11-12(목) 11:00" in seeds[0].detail
    key = seeds[0].key
    explorer.findings.write(_finding(key, NOW - timedelta(hours=2)))
    assert explorer.seeds(NOW) == []  # 하루 한 번
    explorer.findings.write(_finding(key, NOW - timedelta(days=2)))
    assert key not in {s.key for s in explorer.seeds(NOW + timedelta(days=1))}  # 두 번까지


def test_follow_up_questions_become_one_seed_with_what_came_before(tmp_path: Path) -> None:
    """서버 실측: "그래서 봐 못 봐?" 같은 뒷질문이 혼자 씨앗이 되어 찾을 게 없었다("없음")."""
    minute = 1 / 60
    checks = [
        _check("가나은행 하반기 채용 공고 알려줘", hours_ago=3),
        _check(
            "그 공고문 봐봐",
            blocked="페이지를 못 읽음",
            hours_ago=3 - minute,
            missing="스크립트로 그리는 페이지를 브라우저로 읽기",
        ),
        # 찾아보는 답을 읽고 다시 묻는 데 15분 — 질문 사이로 20분까지 한 대화(2차 리뷰)
        _check("그래서 봐 못 봐?", blocked="있는 기능을 안 씀", hours_ago=3 - 16 * minute),
        # 따로 떨어진 대화 — "다 함"으로 끝났으니 풀린 것
        _check("자격증 시험 장소", hours_ago=2),
        _check("고마워 됐어", blocked="없음", result="다 함", hours_ago=2 - 3 * minute),
        # 앞 대화와 30분 넘게 떨어진 질문은 따로 — 여기선 아직 한 시간이 안 됐다
        _check("다른 질문", hours_ago=0.5),
    ]
    seeds = _explorer(tmp_path, checks=checks).seeds(NOW)
    assert len(seeds) == 1
    seed = seeds[0]
    assert seed.subject == "가나은행 하반기 채용 공고 알려줘 → 그 공고문 봐봐 → 그래서 봐 못 봐?"
    assert "물어본 때: 2026-11-12(목) 11:16" in seed.detail
    assert "페이지를 못 읽음" in seed.detail and "있는 기능을 안 씀" in seed.detail
    assert "스크립트로 그리는 페이지를 브라우저로 읽기" in seed.detail
    assert seed.key == f"retry:{stamp(checks[2].at)}"  # 대화의 마지막 질문으로 센다


def _searches(tmp_path: Path, texts: List[str]) -> SQLiteTraceStore:
    traces = SQLiteTraceStore(tmp_path / "t.db")
    traces.write(
        [
            Trace(source="mac_chrome", kind="web_visit", text=t, at=NOW - timedelta(hours=i + 1))
            for i, t in enumerate(texts)
        ]
    )
    return traces


def test_free_seeds_are_only_searches_and_private_bits_never_go_out(tmp_path: Path) -> None:
    traces = _searches(
        tmp_path,
        [
            "검색: 정처기 실기 일정",
            "받은편지함 - 홍길동 - 메일",  # 페이지 제목은 쓰지 않는다
            "검색: hong@example.com 비밀번호",  # 개인 정보 꼴
            "검색: 010-1234-5678 누구",
            "검색: https://example.com/a",
        ],
    )
    explorer = _explorer(tmp_path)
    explorer.traces = traces
    assert explorer.seeds(NOW) == []  # 검색 흔적의 종류·앞말(섹터의 것)을 안 받았다
    explorer.search_kind, explorer.search_prefix = "web_visit", "검색: "
    seeds = explorer.seeds(NOW)
    assert [s.subject for s in seeds] == ["정처기 실기 일정"]
    assert clean("연락처 010-1234-5678, a@b.com") == "연락처 (지움), (지움)"
    explorer.findings.write(_finding(seeds[0].key, NOW - timedelta(days=5), kind=FREE))
    assert explorer.seeds(NOW) == []  # 본 검색어는 한 번만


def test_interest_seeds_rest_six_hours_and_carry_the_memo_date(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    traces = SQLiteTraceStore(db)
    traces.write(
        [
            Trace(source="mac_chrome", kind="web_visit", text=f"{w}{d}", at=NOW - timedelta(days=d))
            for w, days in (("자격증", (0.5, 1.5, 2.5)), ("여행", (20.5, 21.5, 22.5, 23.5)))
            for d in days
        ]
    )
    ids = {t.text: t.id for t in traces.recent("web_visit", NOW - timedelta(days=60)) if t.id}
    topics = SQLiteTopicStore(db)
    ops = [
        Op(
            "create",
            ref=name,
            name=name,
            evidence=tuple(i for t, i in ids.items() if t.startswith(name)),
        )
        for name in ("자격증", "여행")
    ]
    ops.append(Op("note", topic="자격증", text="원서 접수가 다가온다"))
    topics.apply(plan(topics.load(), ops, set(ids.values()), NOW), NOW)
    explorer = _explorer(tmp_path)
    explorer.topics = topics
    seeds = {s.subject: s for s in explorer.seeds(NOW) if s.kind == INTEREST}
    assert seeds["자격증"].weight == 3.0  # 뜨는 중
    assert seeds["자격증"].detail == "메모(11/12): 원서 접수가 다가온다"  # 메모 쓴 날(1차 리뷰)
    explorer.findings.write(_finding(seeds["자격증"].key, NOW - timedelta(hours=2), INTEREST))
    assert "자격증" not in {s.subject for s in explorer.seeds(NOW)}
    assert "자격증" in {s.subject for s in explorer.seeds(NOW + timedelta(hours=5))}


def test_kinds_get_their_share_whatever_their_seed_counts() -> None:
    """1차 리뷰 모의: 본 페이지 300개가 하루 96번 중 72%를 가져갔다 — 갈래를 먼저 몫대로 고른다."""
    seeds = [Seed(INTEREST, f"i{n}", "관심") for n in range(3)]
    seeds += [Seed(FREE, f"f{n}", "검색") for n in range(300)]
    seeds += [Seed(RETRY, "r", "질문")]
    explorer = Explorer(
        reasoner=FakeJudge(""), tools=cast(Any, FakeTools()), findings=cast(Any, None)
    )
    explorer.rng = random.Random(3)
    picks = [explorer.pick(seeds) for _ in range(1000)]
    share = {
        k: sum(1 for p in picks if p is not None and p.kind == k) / 1000
        for k in (INTEREST, RETRY, FREE)
    }
    assert 0.55 < share[INTEREST] < 0.65 and 0.15 < share[FREE] < 0.25
    assert explorer.pick([]) is None
    only_free = [Seed(FREE, "f", "검색")]
    assert explorer.pick(only_free) == only_free[0]  # 빈 갈래는 빠진다


# ── 한 번 알아보기 ────────────────────────────────────────────


def _one_seed(tmp_path: Path, **kw: Any) -> Explorer:
    return _explorer(tmp_path, checks=[_check("가나은행 언제 마감")], **kw)


def test_explore_once_saves_facts_dated_from_the_material_queries_sources_and_the_seed(
    tmp_path: Path,
) -> None:
    tools = FakeTools()
    explorer = _one_seed(tmp_path, tools=tools)
    finding = asyncio.run(explorer.explore_once(NOW))
    assert finding is not None and finding.id is not None
    assert (finding.kind, finding.subject, finding.worth) == (RETRY, "가나은행 언제 마감", TELL)
    assert finding.facts == (
        Fact("가나은행 하반기 서류 마감", date(2026, 11, 16), "가나은행 채용", EVIDENCE),
    )
    assert finding.queries == ("가나은행 채용 일정",)
    assert finding.sources == ("https://example.com/recruit",)
    assert explorer.findings.since(NOW - timedelta(hours=1))[0].facts == finding.facts
    system, user = tools.asked[0]
    assert "개인 사정은 빼고" in system["content"]
    assert "[씨앗 — 못 한 답] 가나은행 언제 마감" in user["content"]
    judge = cast(FakeJudge, explorer.reasoner)
    assert judge.prompts[0].startswith("[오늘] 2026-11-12(목)")  # 오늘은 코드가 준다
    assert f'1. 2026-11-16(월) — "{EVIDENCE}"' in judge.prompts[0]  # 날짜표


def test_dates_the_research_model_wrote_are_not_material(tmp_path: Path) -> None:
    """2차 리뷰: 찾기 모델이 "내일 마감"을 "마감 2026-11-13"으로 바꿔 쓰면 그 글이
    대조 재료라 통과했다."""
    page = "2026.11.05 작성 … 내일 마감"
    tools = FakeTools(result=_found(text="가나은행 마감 2026-11-13", material=(page,)))
    explorer = _one_seed(
        tmp_path, tools=tools, judge=_json(facts=[{"text": "가나은행 마감", "when": 1}])
    )
    finding = asyncio.run(explorer.explore_once(NOW))
    assert finding is not None and [(f.text, f.day) for f in finding.facts] == [
        ("가나은행 마감", None)
    ]
    assert "[날짜표] 없음" in cast(FakeJudge, explorer.reasoner).prompts[0]


def test_recent_queries_are_shown_so_they_are_not_repeated(tmp_path: Path) -> None:
    tools = FakeTools()
    explorer = _one_seed(tmp_path, tools=tools)
    explorer.findings.write(
        Finding(NOW - timedelta(hours=5), FREE, "free:1", "s", "", (), (), ("정처기 실기",), KEEP)
    )
    asyncio.run(explorer.explore_once(NOW))
    assert "[최근 검색어] 정처기 실기" in tools.asked[0][1]["content"]


def test_unsearched_or_empty_research_is_not_a_finding(tmp_path: Path) -> None:
    explorer = _one_seed(tmp_path, tools=FakeTools(result=_found(searched=False)))
    finding = asyncio.run(explorer.explore_once(NOW))
    assert finding is not None and (finding.worth, finding.note) == (NOTHING, UNSEARCHED)
    quiet = _one_seed(tmp_path / "q", tools=FakeTools(result=_found(text="없음")))
    nothing = asyncio.run(quiet.explore_once(NOW))
    assert nothing is not None and (nothing.worth, nothing.note) == (NOTHING, "")
    assert cast(FakeJudge, quiet.reasoner).prompts == []  # 판단을 부르지도 않는다


def test_slow_or_failing_searches_are_kept_with_their_reason(tmp_path: Path) -> None:
    slow = _one_seed(tmp_path, tools=FakeTools(delay=0.2))
    slow.limit = timedelta(seconds=0.05)
    finding = asyncio.run(slow.explore_once(NOW))
    assert finding is not None and finding.note == TIMEOUT
    broken = _one_seed(tmp_path / "b", tools=FakeTools(fail=RuntimeError("터짐")))
    failed = asyncio.run(broken.explore_once(NOW))
    assert failed is not None and (failed.worth, failed.note) == (NOTHING, "오류")
    judge_broken = _one_seed(tmp_path / "j")
    judge_broken.reasoner = FakeJudge("", fail=RuntimeError("거절"))
    judged = asyncio.run(judge_broken.explore_once(NOW))
    assert judged is not None and judged.note == "오류"


def test_an_unreachable_brain_burns_no_seed_and_lets_the_loop_back_off(tmp_path: Path) -> None:
    """1차 리뷰: 두뇌가 꺼진 동안 10분마다 씨앗을 태웠다 — 남기지 않고 올린다."""
    down = _one_seed(tmp_path, tools=FakeTools(fail=httpx.ConnectError("닿지 않음")))
    with pytest.raises(httpx.ConnectError):
        asyncio.run(down.explore_once(NOW))
    assert down.findings.since(NOW - timedelta(days=1)) == []


def test_a_blocked_search_burns_no_seed_either(tmp_path: Path) -> None:
    """2차 리뷰: 검색이 막히면 "검색 없이 씀"으로 남아 그 씨앗을 30일 동안 다시 안 뽑았다."""
    blocked = Researched(text="못 찾았다", uses=(Use("search", "q", ok=False, note="503"),))
    explorer = _one_seed(tmp_path, tools=FakeTools(result=blocked))
    with pytest.raises(SearchDown):
        asyncio.run(explorer.explore_once(NOW))
    assert explorer.findings.since(NOW - timedelta(days=1)) == []


def test_the_browser_gets_a_longer_limit_than_its_own(tmp_path: Path) -> None:
    """1차 리뷰: 탐색 상한(180초)이 브라우저 상한(210초)보다 짧아 찾은 것을 통째로 버렸다."""
    tools = FakeTools(delay=0.2, browse=object())
    explorer = _one_seed(tmp_path, tools=tools)
    explorer.limit = timedelta(seconds=0.05)
    explorer.browse_limit = timedelta(seconds=1)
    finding = asyncio.run(explorer.explore_once(NOW))
    assert finding is not None and finding.note == ""


def test_the_daily_search_cap_stops_exploring_until_tomorrow(tmp_path: Path) -> None:
    explorer = _one_seed(tmp_path)
    explorer.daily_searches = 2
    for hour in (1, 2):
        explorer.findings.write(
            Finding(
                NOW - timedelta(hours=hour), FREE, f"free:{hour}", "s", "", (), (), ("q",), KEEP
            )
        )
    assert asyncio.run(explorer.explore_once(NOW)) is None
    tomorrow = datetime(2026, 11, 12, 15, 30, tzinfo=timezone.utc)  # 한국 11/13 00:30
    assert asyncio.run(explorer.explore_once(tomorrow)) is not None


def test_no_seed_means_no_search(tmp_path: Path) -> None:
    tools = FakeTools()
    explorer = _explorer(tmp_path, tools=tools)
    assert asyncio.run(explorer.explore_once(NOW)) is None and tools.asked == []


# ── 5차 리뷰: 사실 글 거르기 ──────────────────────────────────


def test_a_fact_on_an_unsure_day_may_not_state_its_year_or_weekday() -> None:
    """5차 리뷰: 해 모름 날을 고른 사실 글에 모델이 쓴 해·요일이 그대로 남았다."""
    choices = date_table(("서류 마감 11월 16일 18시까지",), TODAY)
    assert [d.sure for d in choices] == [False]
    raw = _json(
        facts=[
            {"text": "2026년 11월 16일 서류 마감", "when": 1},
            {"text": "11월 16일(월) 서류 마감", "when": 1},
            {"text": "2026.11.16 서류 마감", "when": 1},
            {"text": "11월 16일 서류 마감", "when": 1},
            {"text": "11/16 서류 마감", "when": 1},
        ]
    )
    assert [f.text for f in parse_judged(raw, TODAY, choices).facts] == [
        "11월 16일 서류 마감",
        "11/16 서류 마감",
    ]


def test_slash_dates_in_a_fact_are_checked_but_fractions_are_not() -> None:
    """5차 리뷰: "반값(1/2)"을 살리려고 요일 없는 "11/20"을 통째로 안 봤다."""
    choices = date_table((EVIDENCE,), TODAY)  # 1. 2026-11-16(월)
    raw = _json(
        facts=[
            {"text": "가나은행 면접 11/16", "when": None},
            {"text": "서류 마감 11/13, 면접 11/20", "when": 1},
            {"text": "서류 마감 11/16", "when": 1},
            {"text": "가나카드 반값(1/2) 할인", "when": None},
            {"text": "1/3 가격에 판매", "when": None},
        ],
        title="11/20까지 가나카드 캐시백",
    )
    judged = parse_judged(raw, TODAY, choices)
    assert [f.text for f in judged.facts] == [
        "서류 마감 11/16",
        "가나카드 반값(1/2) 할인",
        "1/3 가격에 판매",
    ]
    assert judged.title == ""


@pytest.mark.parametrize(
    "text",
    [
        "매주 화·목요일 할인",
        "매주 화요일과 목요일 할인",
        "고객센터 운영 월~금요일 9시~18시",
        "주말(토요일·일요일) 2배 적립",
        "수요일마다 10% 할인",
        "토요일 휴무",
        "매월 첫째 주 월요일 휴관",
        "카드 대금 매월 15일까지 납부하면 할인",
        "매월 1일 오전 10시 선착순 쿠폰",
        "매달 1일부터 선착순 적립",
        "구매확정 7일 후 포인트 지급",
        "이용일 3일 전까지 취소하면 전액 환불",
        "출국 3일 전까지 신청",
        "가입 후 24시간 후 자동 해지",
    ],
)
def test_recurring_and_event_relative_benefits_are_kept(text: str) -> None:
    """5차 리뷰: 되풀이하는 요일·날, 다른 일 기준의 기간이 든 혜택 사실이 버려졌다(JAR-048 의
    재료)."""
    raw = _json(worth=KEEP, facts=[{"text": text, "when": None}])
    assert [f.text for f in parse_judged(raw, TODAY).facts] == [text]


@pytest.mark.parametrize(
    "text", ["발표는 3일 뒤", "3일 뒤 발표", "월요일 마감", "16일 발표", "16일 개강"]
)
def test_reading_day_words_and_loose_days_are_still_dropped(text: str) -> None:
    raw = _json(worth=KEEP, facts=[{"text": text, "when": None}])
    assert parse_judged(raw, TODAY).facts == ()


# ── 6차 리뷰: 사실 글 거르기 ──────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "가나 설명회 (11/20) 개최",  # 괄호 날짜는 분수가 아니다
        "가나마트 11/20 할인 행사",
        "팝업스토어 금요일 오픈, 2주간 운영",  # "운영"이 요일 곁에 없다
        "영업점 방문 고객 대상 이벤트 금요일 마감",
        "지금부터 3일 뒤 접수 마감",
        "단 3일 뒤 종료",
    ],
)
def test_more_reading_day_dates_are_dropped(text: str) -> None:
    raw = _json(worth=KEEP, facts=[{"text": text, "when": None}])
    assert parse_judged(raw, TODAY).facts == ()


@pytest.mark.parametrize(
    "text",
    [
        "3/4분기 영업이익 증가",
        "1/4분기 실적 발표",
        "총 3일 진행",
        "결제하면 3일 후 포인트 적립",
        "가입하면 7일 후 쿠폰",
        "신청하고 3일 뒤 발급",
    ],
)
def test_quarters_counts_and_conditional_benefits_are_kept(text: str) -> None:
    raw = _json(worth=KEEP, facts=[{"text": text, "when": None}])
    assert [f.text for f in parse_judged(raw, TODAY).facts] == [text]


def test_a_weekday_in_brackets_on_an_unsure_day_is_dropped() -> None:
    choices = date_table(("서류 마감 11월 16일 18시까지",), TODAY)
    raw = _json(
        facts=[{"text": "서류 16일(월) 마감", "when": 1}, {"text": "서류 16일 마감", "when": 1}]
    )
    assert [f.text for f in parse_judged(raw, TODAY, choices).facts] == ["서류 16일 마감"]
