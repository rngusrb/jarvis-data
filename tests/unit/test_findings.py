"""알아본 것 — 저장소, 신호, 맥락, 날짜 옆 거리(JAR-046)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.brain.providers import FindingsProvider
from src.core.findings import KEEP, NOTHING, TELL, Fact, Finding, day_label, when_label
from src.storage.findings import SQLiteFindings
from src.triggers.findings import FindingWatch

NOW = datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc)  # 한국 11/12(목) 14:00
QUOTE = "가나은행 하반기 서류 마감 2026.11.16(월) 18:00"


def _f(
    at: datetime,
    worth: str = TELL,
    note: str = "",
    facts: tuple = (Fact("가나은행 서류 마감", date(2026, 11, 16), "가나은행 채용", QUOTE),),
    key: str = "interest:1",
) -> Finding:
    return Finding(
        at=at,
        kind="관심사",
        key=key,
        subject="취업 > 금융권",
        summary="가나은행 하반기 서류 마감",
        facts=facts,
        sources=("https://example.com",),
        queries=("가나은행 채용",),
        worth=worth,
        idea="채용 달력",
        note=note,
    )


def test_day_label_is_counted_by_code() -> None:
    today = date(2026, 11, 12)
    assert day_label(date(2026, 11, 16), today) == "11/16(월), 4일 뒤"
    assert day_label(today, today) == "11/12(목), 오늘"
    assert day_label(date(2026, 11, 13), today) == "11/13(금), 내일"
    assert day_label(date(2026, 11, 14), today) == "11/14(토), 모레"
    assert day_label(date(2026, 11, 11), today) == "11/11(수), 어제"
    assert day_label(date(2026, 11, 9), today) == "11/09(월), 3일 지남"
    assert day_label(date(2027, 1, 2), date(2026, 12, 31)) == "01/02(토), 모레"  # 해 넘김


def test_a_dated_fact_shows_the_line_its_date_came_from() -> None:
    """날짜는 코드가 원자료에서 뽑았다 — 다른 일에 잘못 붙였으면 원문이 같이 보여
    드러난다(2차 리뷰)."""
    today = date(2026, 11, 12)
    fact = Fact("가나은행 서류 마감", date(2026, 11, 16), "가나은행 채용", QUOTE)
    assert when_label(fact, today) == f'11/16(월), 4일 뒤 — 원문 "{QUOTE}"'
    assert when_label(fact, today, quote=10) == '11/16(월), 4일 뒤 — 원문 "가나은행 하반기 서…"'
    assert when_label(Fact("날짜만", date(2026, 11, 16)), today) == "11/16(월), 4일 뒤"
    assert when_label(Fact("날짜 없음"), today) == ""


def test_store_round_trip_and_what_the_loop_counts(tmp_path: Path) -> None:
    store = SQLiteFindings(tmp_path / "t.db")
    assert store.last_at() is None
    first = store.write(_f(NOW - timedelta(hours=3)))
    store.write(_f(NOW - timedelta(hours=1), worth=NOTHING, note="시간 초과", key="free:9"))
    found = store.since(NOW - timedelta(days=1))
    assert [f.id for f in found][0] == first and found[0].facts == _f(NOW).facts
    assert found[0].queries == ("가나은행 채용",) and found[0].idea == "채용 달력"
    assert store.last_at() == NOW - timedelta(hours=1)
    assert store.explored(NOW - timedelta(days=1)) == {
        "interest:1": NOW - timedelta(hours=3),
        "free:9": NOW - timedelta(hours=1),
    }
    assert store.tries("interest:1") == 1 and store.tries("free:9") == 0  # 실패는 안 센다
    assert store.queries_since(NOW - timedelta(days=1)) == ["가나은행 채용", "가나은행 채용"]


def _with_ids(store: SQLiteFindings, *items: Finding) -> list:
    for item in items:
        store.write(item)
    return store.since(NOW - timedelta(days=3))


def test_watch_tells_only_todays_good_findings_in_waking_hours(tmp_path: Path) -> None:
    store = SQLiteFindings(tmp_path / "t.db")
    found = _with_ids(
        store,
        _f(NOW - timedelta(hours=2)),  # 오늘 12:00
        _f(NOW - timedelta(hours=1), worth=KEEP),
        _f(NOW - timedelta(minutes=30), note="상대 날짜 말"),
        _f(NOW - timedelta(days=1)),  # 어제 — 말은 그날 새로 써야 한다
    )
    insights = FindingWatch().check(found, NOW)
    assert len(insights) == 1
    insight = insights[0]
    noon = next(f for f in found if f.at == NOW - timedelta(hours=2))
    assert insight.trigger == f"finding:{noon.id}" and insight.family == "finding"
    assert (
        f'- 가나은행 서류 마감 — 11/16(월), 4일 뒤 — 원문 "{QUOTE}" (출처: 가나은행 채용)'
        in insight.summary
    )
    assert "알아본 때 12:00" in insight.summary
    night = datetime(2026, 11, 12, 13, 0, tzinfo=timezone.utc)  # 한국 22:00
    assert FindingWatch().check(found, night) == []


def test_context_shows_kept_findings_with_distances_from_today(tmp_path: Path) -> None:
    store = SQLiteFindings(tmp_path / "t.db")
    assert FindingsProvider(source=store).fetch(None, NOW) is None
    store.write(_f(NOW - timedelta(days=2), worth=KEEP))
    store.write(_f(NOW - timedelta(days=3), worth=TELL, key="interest:2"))
    store.write(_f(NOW - timedelta(days=1), worth=NOTHING))
    store.write(_f(NOW - timedelta(hours=1), note="오류"))
    block = FindingsProvider(source=store).fetch(None, NOW)
    assert block is not None
    lines = block.body.splitlines()
    # 알릴 것이 먼저(사흘 전이어도), 그다음 기록만. 모델이 쓴 긴 글 없이 제목과 사실만.
    assert lines[0] == "- 11/09 관심사 — 취업 > 금융권: 가나은행 하반기 서류 마감"
    assert lines[1] == f'  · 가나은행 서류 마감 (11/16(월), 4일 뒤 — 원문 "{QUOTE}")'
    assert lines[2] == "- 11/10 관심사 — 취업 > 금융권: 가나은행 하반기 서류 마감"
    assert len(lines) == 4  # 없음·실패는 뺀다
    # 사흘 뒤에 보면 거리도 그날에 맞춰 바뀐다
    later = FindingsProvider(source=store).fetch(None, NOW + timedelta(days=3))
    assert later is not None and "(11/16(월), 내일 — 원문" in later.body


def test_a_fact_without_a_sure_year_keeps_that_through_the_store_and_the_signal(
    tmp_path: Path,
) -> None:
    """해 모름은 저장을 지나 신호·맥락까지 — 거리 없이 원문만, 신호엔 단정하지 말라는 말까지
    (4차 리뷰: 저장 왕복 시험이 없었다)."""
    unsure = Fact("가나은행 서류 마감", None, "가나은행 채용", "서류 마감 11월 16일", no_year=True)
    store = SQLiteFindings(tmp_path / "t.db")
    store.write(_f(NOW - timedelta(hours=1), facts=(unsure,)))
    [found] = store.since(NOW - timedelta(days=1))
    assert found.facts == (unsure,)
    [insight] = FindingWatch().check([found], NOW)
    assert '- 가나은행 서류 마감 — 해 모름 — 원문 "서류 마감 11월 16일"' in insight.summary
    assert "언제라고 단정하지 않는다" in insight.summary
    block = FindingsProvider(source=store).fetch(None, NOW)
    assert block is not None and '(해 모름 — 원문 "서류 마감 11월 16일")' in block.body
