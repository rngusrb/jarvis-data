"""자기 점검 — 보낸 답을 돌아보고 정해진 꼴로 남기기(JAR-039), 모아서 부족한 기능 말하기
(JAR-040)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import pytest

from src.brain.selfcheck import (
    FAILED,
    OTHER,
    TIME,
    GapReport,
    SelfReview,
    parse_check,
)
from src.brain.tools import Use
from src.storage.selfchecks import SelfCheck, SQLiteSelfChecks

NOW = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)


@dataclass
class Fixed:
    reply: str = ""
    error: Optional[Exception] = None
    prompts: List[str] = field(default_factory=list)
    systems: List[Optional[str]] = field(default_factory=list)

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        self.prompts.append(prompt)
        self.systems.append(system)
        if self.error is not None:
            raise self.error
        return self.reply


def _judged(**values: str) -> str:
    base = {
        "result": "일부",
        "blocked": "페이지를 못 읽음",
        "note": "일정표가 빈 틀",
        "missing": "",
    }
    base.update(values)
    return "```json\n" + json.dumps(base, ensure_ascii=False) + "\n```"


def test_a_sent_answer_is_looked_back_on_and_kept(tmp_path: Path) -> None:
    store = SQLiteSelfChecks(tmp_path / "t.db")
    brain = Fixed(_judged(missing="로그인된 브라우저"))
    uses = (
        Use("search", "정처기 실기 접수", True, "결과 5개", 5),
        Use("read", "https://www.q-net.or.kr/a", False, "본문이 비었다(111자)"),
    )
    check = asyncio.run(
        SelfReview(brain, store).review("정처기 실기 언제야?", "공지엔 없어.", uses, NOW)
    )

    assert check is not None and check.result == "일부" and check.blocked == "페이지를 못 읽음"
    prompt = brain.prompts[0]
    assert "정처기 실기 언제야?" in prompt and "공지엔 없어." in prompt
    assert "- 검색 정처기 실기 접수 → 결과 5개" in prompt
    assert "- 읽기 https://www.q-net.or.kr/a → 실패: 본문이 비었다(111자)" in prompt
    assert "JSON 하나로만" in str(brain.systems[0]) and "(도구 없음" in prompt
    [kept] = store.since(NOW - timedelta(days=1))
    assert kept.missing == "로그인된 브라우저" and kept.tools[1][2] is False


@pytest.mark.parametrize(
    "reply",
    [
        "다 했어!",
        _judged(result="대충"),
        _judged(blocked="모르는 갈래"),
        '{"result": "일부", "blocked": "기타", "note": 3}',
        "[1, 2]",
    ],
)
def test_a_judgment_that_does_not_fit_is_not_kept(
    tmp_path: Path, reply: str, caplog: pytest.LogCaptureFixture
) -> None:
    """깨진 판단은 판단이 아니라 실패다 — 지어 채우지 않고 원문을 경고로 남긴다(brain/_GUIDE)."""
    store = SQLiteSelfChecks(tmp_path / "t.db")
    check = asyncio.run(SelfReview(Fixed(reply), store).review("q", "a", (), NOW))
    assert check is None and store.since(NOW - timedelta(days=1)) == []
    assert "자기 점검 판단을 못 읽었다" in caplog.text


def test_parse_reads_the_fixed_form_only() -> None:
    assert parse_check('앞말 {"result": "다 함", "blocked": "없음"} 뒷말') == (
        "다 함",
        "없음",
        "",
        "",
    )
    assert parse_check('{"result": "못 함", "blocked": "기타", "note": null}') == (
        "못 함",
        "기타",
        "",
        "",
    )
    assert parse_check("") is None


def test_answers_that_failed_outright_are_kept_without_the_model(tmp_path: Path) -> None:
    store = SQLiteSelfChecks(tmp_path / "t.db")
    review = SelfReview(Fixed(), store)
    review.failed("q", "찾다가 6분 상한을 넘겨 멈췄다", TIME, NOW)
    review.failed("q2", "답이 비었다", OTHER, NOW)
    kept = store.since(NOW - timedelta(days=1))
    assert [(k.result, k.blocked) for k in kept] == [(FAILED, TIME), (FAILED, OTHER)]
    with pytest.raises(ValueError):
        review.failed("q", "?", "모르는 갈래", NOW)


def _keep(store: SQLiteSelfChecks, question: str, result: str, blocked: str, days: int = 0) -> None:
    store.add(
        SelfCheck(
            at=NOW - timedelta(days=days),
            question=question,
            answer="a",
            tools=(),
            result=result,
            blocked=blocked,
            missing="로그인" if blocked == "로그인이 필요함" else "",
            note=f"{question} 막힘",
        )
    )


def test_gaps_counts_without_the_model_when_nothing_was_stuck(tmp_path: Path) -> None:
    store = SQLiteSelfChecks(tmp_path / "t.db")
    report = GapReport(Fixed("쓰면 안 됨"), store)
    assert report.tally(NOW) == ("최근 14일 동안 돌아본 답이 없어.", [])
    _keep(store, "안녕", "다 함", "없음")
    _keep(store, "옛 질문", "못 함", "기타", days=20)  # 14일 밖
    tally, problems = report.tally(NOW)
    assert problems == [] and "답 1개" in tally and "막힌 답은 없었어" in tally


def test_gaps_counts_kinds_with_examples_and_adds_what_is_missing(tmp_path: Path) -> None:
    store = SQLiteSelfChecks(tmp_path / "t.db")
    for q in ("은행 잔고", "카드 내역"):
        _keep(store, q, "못 함", "로그인이 필요함")
    _keep(store, "정처기 일정", "일부", "페이지를 못 읽음")
    _keep(store, "안녕", "다 함", "없음")
    brain = Fixed("- 로그인이 필요한 사이트 2번(예: 카드 내역) — 로그인 세션 쓰기")
    report = GapReport(brain, store)

    tally, problems = report.tally(NOW)
    assert "답 4개" in tally and "다 함 1 · 일부 1 · 못 함 2" in tally
    assert "- 로그인이 필요함 2번 — 예: “카드 내역”" in tally
    assert "- 페이지를 못 읽음 1번" in tally and len(problems) == 3
    text = asyncio.run(report.write(tally, problems, abilities=["read_page: 첨부까지 읽는다"]))
    assert text.startswith(tally) and "부족한 기능:\n- 로그인이 필요한 사이트" in text
    assert "있었으면 한 기능: 로그인" in brain.prompts[0]
    assert brain.prompts[0].startswith("[지금 있는 기능]\n- read_page: 첨부까지 읽는다")
    assert "새로 만들 것으로 쓰지 않는다" in str(brain.systems[0])


@pytest.mark.parametrize("brain", [Fixed(error=RuntimeError("down")), Fixed("   ")])
def test_gaps_still_sends_the_counts_when_the_write_up_fails(
    tmp_path: Path, brain: Fixed, caplog: pytest.LogCaptureFixture
) -> None:
    store = SQLiteSelfChecks(tmp_path / "t.db")
    _keep(store, "은행 잔고", "못 함", "로그인이 필요함")
    report = GapReport(brain, store)
    tally, problems = report.tally(NOW)
    text = asyncio.run(report.write(tally, problems))
    assert text.startswith(tally) and "정리" in text and "부족한 기능 정리" in caplog.text


def test_what_jarvis_can_already_do_is_shown_so_unused_is_not_called_missing(
    tmp_path: Path,
) -> None:
    """있는 기능을 몰라서 이미 있는 첨부 읽기를 "있었으면 할 기능"으로 적었다(2026-11-09 실측)."""
    store = SQLiteSelfChecks(tmp_path / "t.db")
    brain = Fixed(_judged(blocked="있는 기능을 안 씀"))
    check = asyncio.run(
        SelfReview(brain, store).review(
            "q", "첨부는 못 열었어.", (), NOW, abilities=["read_page: 첨부까지 읽는다"]
        )
    )
    assert check is not None and check.blocked == "있는 기능을 안 씀"
    assert "[지금 있는 기능]\n- read_page: 첨부까지 읽는다" in brain.prompts[0]
    assert "우회하는 것은 쓰지 않는다" in str(brain.systems[0])
