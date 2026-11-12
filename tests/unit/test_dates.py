"""자료에 적힌 날짜를 코드가 뽑는다(JAR-046 2차 리뷰 — 모델이 날짜를 쓰지 않게)."""

from __future__ import annotations

from datetime import date
from typing import List, Tuple

import pytest

from src.brain.dates import dates_in

TODAY = date(2026, 11, 12)  # 목요일


def _days(text: str) -> List[Tuple[str, bool]]:
    return [(d.day.isoformat(), d.written) for d in dates_in(text, TODAY)]


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("서류 마감 2026.11.16(월) 18:00", "2026-11-16"),
        ("서류 마감 2026-11-16", "2026-11-16"),
        ("서류 마감 2026/11/16", "2026-11-16"),
        ("서류 마감 2026년 11월 16일", "2026-11-16"),
        ("서류 마감 2026. 11. 16.(월)", "2026-11-16"),
        ("서류 마감 11월 16일", "2026-11-16"),
        ("서류 마감 11/16", "2026-11-16"),
        ("서류 마감 11.16(월)", "2026-11-16"),
        ("시험 1월 5일", "2027-01-05"),  # 오늘에 가까운 해 — 해를 넘긴다
    ],
)
def test_the_shapes_dates_are_written_in(text: str, day: str) -> None:
    assert _days(text) == [(day, False)]


def test_a_years_comes_from_the_text_or_the_weekday_not_from_today() -> None:
    """2차 리뷰: "2025. 11. 16." 이 2026-11-16 으로 통과했다."""
    assert _days("정보처리기사 실기 2025. 11. 16.(일) 시행") == [("2025-11-16", False)]
    # 다른 해 언급은 해로 쓰지 않는다 — 해를 모른다고 둔다(4차 리뷰 뒤 사용자 결정)
    assert _all("2025년 결과 발표 ... 실기 11월 16일") == [("2026-11-16", False, False)]
    assert _days("11.16(일) 마감") == [("2025-11-16", False)]  # 2026-11-16 은 월요일
    assert _days("2026.11.16(일) 마감") == []  # 적힌 연도와 요일이 어긋난다


def test_the_day_an_article_was_written_is_marked() -> None:
    """2차 리뷰: 기사 "입력 2026.11.12" 가 "11월 중순" 면접의 날로 남았다."""
    text = "가나은행 하반기 채용 — 입력 2026.11.12 09:30\n면접은 11월 중순에 진행될 예정이다."
    assert _days(text) == [("2026-11-12", True)]  # "11월 중순"은 날이 아니다
    assert _days("2026. 11. 12. — 가나은행이 공채를 연다. 서류는 11월 16일까지") == [
        ("2026-11-12", True),  # 검색 요약 맨 앞
        ("2026-11-16", False),
    ]
    assert _days("작성일 2026.11.03 / 2026.11.12 09:30 송고") == [
        ("2026-11-03", True),
        ("2026-11-12", True),
    ]


def test_the_same_words_before_a_task_date_are_not_a_stamp() -> None:
    text = "정보 입력 11/16까지, 등록 마감 2026.11.20, 합격자 게시 11/20(금)"
    assert _days(text) == [("2026-11-16", False), ("2026-11-20", False)]


def test_both_ends_of_a_span_and_spans_over_new_year() -> None:
    assert _days("원서접수 2026.11.16~11.19, 실기 11월 22일~23일") == [
        ("2026-11-16", False),
        ("2026-11-19", False),
        ("2026-11-22", False),
        ("2026-11-23", False),
    ]
    assert _days("이벤트 12/28~1/3") == [("2026-12-28", False), ("2027-01-03", False)]
    assert _days("접수 2026.11.16 - 18:00 까지") == [("2026-11-16", False)]  # 시각은 끝이 아니다


def test_relative_words_vague_spans_numbers_and_addresses_are_not_dates() -> None:
    assert _days("내일 마감, 이번 주 금요일, 11월 중순, 연말") == []
    assert _days("점유율 3.15% 상승, 09:30 시작, 3/5%") == []
    assert _days("https://news.example.com/2026/11/12/article 참고") == []


def test_the_line_around_a_date_is_kept_as_it_was() -> None:
    long = "가" * 100 + " 서류 마감 2026.11.16(월) 18:00 " + "나" * 100
    [found] = dates_in(long, TODAY)
    assert "서류 마감 2026.11.16(월) 18:00" in found.line
    assert found.line.startswith("…") and found.line.endswith("…") and len(found.line) < 160


def _all(text: str, today: date = TODAY) -> List[Tuple[str, bool, bool]]:
    """(날, 글 쓴 날, 해가 확실한가)."""
    return [(d.day.isoformat(), d.written, d.sure) for d in dates_in(text, today)]


def test_schedule_rows_and_future_notice_days_are_task_dates() -> None:
    """3차 리뷰: 검색 요약 맨 앞의 날짜를 거르는 규칙이 모든 줄에 걸려 일정표(날짜로 시작하는 표의
    행·목록)가 통째로 "글 쓴 날"이 됐다. 앞날은 글을 쓴 날일 수 없다."""
    table = "2026.11.16(월) | 서류 마감\n11월 25일(수) - 면접\n12/1(화) · 최종 발표"
    assert _days(table) == [("2026-11-16", False), ("2026-11-25", False), ("2026-12-01", False)]
    notices = "최종 합격자 공고일: 2026. 12. 1.\n합격자 게시일 11/20(금)"
    assert _days(notices) == [("2026-12-01", False), ("2026-11-20", False)]


def test_todays_deadlines_beside_label_words_are_task_dates() -> None:
    """4차 리뷰: 이름표 말이 옆에 있는 오늘 마감이 "글 쓴 날"로 빠졌다."""
    for text in (
        "정보 입력 11/12까지",
        "11월 12일 등록 마감",
        "합격자 공고 11/12(목)",
        "서류 등록 2026.11.12(목) 18:00까지",
    ):
        assert [w for _, w in _days(text)] == [False], text


def test_bylines_and_their_labels_mark_todays_and_past_dates() -> None:
    assert _days("등록 2026-11-12 09:30 수정 2026-11-12 10:00") == [("2026-11-12", True)]
    assert _days("2026.11.12 09:30 | 가나일보 | 가나은행 하반기 채용 접수 시작") == [
        ("2026-11-12", True)
    ]
    assert _days("작성일 2026.11.03") == [("2026-11-03", True)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 그 글을 쓴 날 기준 — 작년 기사의 "올해"는 작년이다(4차 리뷰 재현)
        (
            "입력 2025.11.03 09:30\n가나은행은 올해 하반기 150명을 채용한다."
            " 서류 접수는 11월 13일까지다.",
            ("2025-11-13", False, True),
        ),
        ("2025. 11. 03. — 가나은행 서류 마감 11월 13일", ("2025-11-13", False, True)),
        ("입력 2026.11.03\n작년 11월 13일엔 마감이 빨랐다", ("2025-11-13", False, True)),
        # 요일이 해를 확인한다 — 다른 해 언급("지난해보다")에 안 흔들린다(4차 리뷰 재현)
        (
            "올해 신입 200명을 뽑는다. 지난해(150명)보다 늘었다."
            " 서류 마감은 11월 16일(월) 오후 6시.",
            ("2026-11-16", False, True),
        ),
        ("2025년 소비자가 뽑은 브랜드 1위\n이벤트 11월 16일(월)", ("2026-11-16", False, True)),
        ("11.16(일) 마감", ("2025-11-16", False, True)),  # 해 없는 작년 글
    ],
)
def test_a_year_is_settled_only_by_the_date_its_weekday_or_the_day_it_was_written(
    text: str, expected: Tuple[str, bool, bool]
) -> None:
    assert expected in _all(text)


@pytest.mark.parametrize(
    "text",
    [
        "[2025 하반기] 가나은행 신입행원 채용 공고\n서류 마감 11월 13일 18시",  # 다른 줄의 해
        "2025 하반기 채용 총정리\n올해 하반기 서류 마감 11월 13일",  # 쓴 날 모르는 "올해"
        "가나은행 신입 채용…지난해보다 50명 늘어 서류 접수는 11월 13일까지",  # 다른 일의 해
        "2027년 상반기 신입 채용 서류 마감 11월 13일",
        "2025-2026 시즌 개막 11월 13일",
        "2027학년도 수능 11월 13일",
        "서류 마감 11월 13일",
    ],
)
def test_a_year_that_is_not_certain_stays_unknown_and_is_kept(text: str) -> None:
    """확실하지 않으면 해를 모른다고 둔다 — 지우지도("지난해보다"에 올해 일정이 사라졌다), 오늘에
    가까운 해로 단정하지도 않는다(사용자 결정: 확실할 때만 날짜로)."""
    assert _all(text) == [("2026-11-13", False, False)]


def test_in_january_a_last_year_label_does_not_erase_this_years_dates() -> None:
    jan = date(2027, 1, 10)
    assert _all("2026년 귀속 연말정산 간소화 서비스 1월 15일 개통", jan) == [
        ("2027-01-15", False, False)
    ]
    assert _all("2026년 4분기 실적 발표 1월 28일(목)", jan) == [("2027-01-28", False, True)]


def test_a_weekday_that_fits_no_year_drops_the_date_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="src.brain.dates"):
        assert _days("서류 마감 11월 16일(화)") == []
    assert "요일이 맞는 해가 없어 버린다" in caplog.text
    # 끝의 요일이 틀렸다 — 끝을 버리고, 그 끝을 따로 다시 잡아 다른 해로 보내지도 않는다
    assert _days("11월 16일(월)~11월 19일(금)") == [("2026-11-16", False)]


def test_span_ends_roll_into_the_next_month_or_year() -> None:
    assert _days("11월 28일~3일") == [("2026-11-28", False), ("2026-12-03", False)]
    assert _days("12월 28일~3일") == [("2026-12-28", False), ("2027-01-03", False)]
    assert _days("11. 16.(월) ~ 19.(목)") == [("2026-11-16", False), ("2026-11-19", False)]
    assert _days("응모 26.11.16(월)~26.11.30(월)") == [("2026-11-16", False), ("2026-11-30", False)]
    assert _days("이벤트 기간 11.16~11.30") == [("2026-11-16", False), ("2026-11-30", False)]
    assert _days("12월 1~10일 추가 이용") == [("2026-12-01", False), ("2026-12-10", False)]


def test_two_digit_years_and_task_words_glued_to_a_date() -> None:
    assert _all("26년 11월 16일 마감") == [("2026-11-16", False, True)]
    for glued in ("11/16개강", "11/16개최", "11/20회의", "11/16점검", "11/20명단 발표"):
        assert len(_days(glued)) == 1, glued


def test_counts_floors_lines_and_addresses_are_not_dates() -> None:
    assert _days("4/5호선, 제12/13회, B1/2F, 11/16명") == []
    assert _days("news.example.com/2026/11/12/abc 기사") == []
    assert _days("마감 2026.11.16/18:00") == [("2026-11-16", False)]  # 주소가 아니다


def test_long_dotted_text_stays_fast() -> None:
    import time

    started = time.perf_counter()
    dates_in("a." * 15000 + "/x", TODAY)
    assert time.perf_counter() - started < 0.5


# ── 5차 리뷰 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "end"),
    [
        ("입력 2026.11.10\n서류 접수는 11월 9일부터 20일까지다.", ("2026-11-20", False, True)),
        ("2026. 11. 10. — 서류 접수는 11월 9일부터 20일까지다.", ("2026-11-20", False, True)),
        ("11월 16일(월)부터 20일(금)까지", ("2026-11-20", False, True)),
        ("11.16(월)~20(금)", ("2026-11-20", False, True)),
        ("11/16(월)~20(금)", ("2026-11-20", False, True)),
        ("응모 기간 11.9(월)~20(금)", ("2026-11-20", False, True)),
        ("11월 9일 오전 10시부터 20일 오후 6시까지", ("2026-11-20", False, False)),
        ("접수 11/9~20, 발표 별도", ("2026-11-20", False, False)),
    ],
)
def test_the_end_of_a_span_is_read_in_its_common_shapes(
    text: str, end: Tuple[str, bool, bool]
) -> None:
    """5차 리뷰 High: "11월 9일부터 20일까지"·"~20(금)"의 마감이 조용히 사라졌다."""
    assert end in _all(text)


def test_a_dash_or_a_counted_day_is_not_a_span() -> None:
    assert _days("11월 16일(월) - 3일간 진행") == [("2026-11-16", False)]
    assert _days("2026.11.16 ~ 2일차 일정") == [("2026-11-16", False)]
    assert _days("11월 28일(토) - 1일 1회 응모") == [("2026-11-28", False)]


def test_a_weekday_at_the_end_or_spelled_out_settles_the_year() -> None:
    assert _all("서류 마감은 11월 16일 월요일 오후 6시까지") == [("2026-11-16", False, True)]
    assert _all("접수기간: 11.16~11.20(금)") == [
        ("2026-11-16", False, True),
        ("2026-11-20", False, True),
    ]


def test_each_line_takes_its_own_or_the_nearest_earlier_byline() -> None:
    """5차 리뷰: 게시판 목록·댓글의 날짜를 그 글을 쓴 날로 잡아 작년 일이 확실한 "내일"이 됐다."""
    board = (
        "2026.11.10 09:00 | 가나은행 채용 설명회 안내\n"
        "2025.11.03 09:00 | 가나은행 신입 채용 공고(서류 11월 13일 마감)"
    )
    assert ("2025-11-13", False, True) in _all(board)
    article = "가나일보 2025.11.03 09:30\n서류 접수는 11월 13일까지다.\n댓글 3\n2025.11.04 10:30"
    assert ("2025-11-13", False, True) in _all(article)
    # 아래 댓글의 해가 다르면 위 바이라인이 본문의 것인지 모른다 — 확실하지 않다(6차 리뷰)
    commented = article.replace("2025.11.04 10:30", "2026.11.11 10:30")
    assert ("2026-11-13", False, False) in _all(commented)
    assert ("2026-11-13", False, True) not in _all(commented)


def test_a_byline_after_an_outlet_name_is_the_day_it_was_written() -> None:
    text = "가나일보 2026.11.12 09:30 가나은행 하반기 채용 접수 시작…서류는 11월 20일까지"
    assert _all(text) == [("2026-11-12", True, True), ("2026-11-20", False, True)]
    assert _all("[가나일보] 2026.11.12 09:30 하반기 채용 접수 시작") == [("2026-11-12", True, True)]


def test_a_year_far_from_the_day_written_needs_a_weekday() -> None:
    """5차 리뷰: 연초에 낸 공고의 11월 일정을 요일이 맞는데도 버렸다."""
    assert ("2026-11-16", False, True) in _all("게시일 2026.01.10\n서류 마감 11월 16일(월)")
    assert ("2026-11-16", False, False) in _all("게시일 2026.01.10\n서류 마감 11월 16일")
    plan = "게시일 2026.02.10\n11.16(월) 수강신청 시작\n11.20(금) 수강신청 마감"
    assert [d for d in _all(plan) if not d[1]] == [
        ("2026-11-16", False, True),
        ("2026-11-20", False, True),
    ]


def test_another_year_in_the_same_sentence_makes_the_day_unsure() -> None:
    """5차 리뷰: "지난해 서류 마감일은 11월 13일이었다"가 올해의 확실한 "내일"이 됐다."""
    text = "입력 2026.11.10\n지난해 서류 마감일은 11월 13일이었다. 올해는 11월 20일까지다."
    assert [d for d in _all(text) if not d[1]] == [
        ("2026-11-13", False, False),
        ("2026-11-20", False, True),
    ]
    assert ("2025-11-20", False, True) in _all("입력 2026.06.01\n지난 11월 20일 열린 행사에서")


def test_todays_events_with_weekdays_or_task_words_are_not_stamps() -> None:
    for text in (
        "합격자 공고: 2026.11.12(목)",
        "서류전형 합격자 공고: 11월 12일(목) 14시",
        "합격자 공고 2026.11.12 14:00",
    ):
        assert [w for _, w in _days(text)] == [False], text


def test_the_end_of_a_span_is_never_the_day_it_was_written() -> None:
    text = "채용 공고: 2026.11.10 ~ 2026.11.20"
    assert _days(text) == [("2026-11-10", True), ("2026-11-20", False)]


# ── 6차 리뷰 ──────────────────────────────────────────────


def test_full_width_tildes_and_glued_dashes_make_spans() -> None:
    assert _all("접수기간 11월 9일(월)～20일(금)") == [
        ("2026-11-09", False, True),
        ("2026-11-20", False, True),
    ]
    assert [d for d, _, _ in _all("12월 1～10일")] == ["2026-12-01", "2026-12-10"]
    assert [d for d, _, _ in _all("이벤트 기간 11.16～11.30")] == ["2026-11-16", "2026-11-30"]
    assert [d for d, _, _ in _all("11월 16-20일")] == ["2026-11-16", "2026-11-20"]
    assert [d for d, _, _ in _all("접수 11.16-11.20")] == ["2026-11-16", "2026-11-20"]


@pytest.mark.parametrize(
    "text",
    [
        "이벤트는 11월 16일(월)부터 1일 1회 응모 가능",
        "가나 축제는 11월 16일(월)부터 3일 동안 열린다",
        "11월 16일(월)부터 7일 이내 수령",
        "11월 16일(월)부터 2일 간격으로",
    ],
)
def test_counted_days_after_a_start_are_not_a_span_end(text: str) -> None:
    """6차 리뷰: 띄어 쓴 "3일 동안"·"1일 1회"가 원문에 없는 기간 끝을 만들었다."""
    assert _days(text) == [("2026-11-16", False)]


def test_a_list_whose_dates_follow_its_items_does_not_lend_them_forward() -> None:
    """6차 리뷰: 날짜 줄이 항목 뒤에 오는 목록에서 앞 항목의 날을 받아 작년 공고가 "내일"이 됐다."""
    board = (
        "인턴 모집(서류 11월 20일 마감)\n가나일보 | 2026.11.10 09:00\n"
        "신입 채용 공고(서류 11월 13일 마감)\n가나일보 | 2025.11.03 09:00"
    )
    assert ("2026-11-13", False, True) not in _all(board)
    assert ("2026-11-13", False, False) in _all(board)
    # 한 줄에 둘이면 늦은 것
    assert ("2026-11-20", False, True) in _all(
        "등록일 2025.10.01 수정일 2026.10.15\n서류 마감 11월 20일"
    )


@pytest.mark.parametrize(
    "line",
    [
        "지난 해 서류 마감은 11월 13일이었다.",
        "전년도 서류 마감은 11월 13일이었다.",
        "[2025 하반기] 가나은행 신입행원 채용 서류 마감 11월 13일",
        "2025학년도 수시 원서 마감 11월 13일",
        "서류 마감은 11월 13일이었던 지난해와 달리 올해는 늦다.",
        "2025.11.03 공고된 채용의 서류 마감은 11월 13일까지였다.",
    ],
)
def test_another_year_anywhere_in_the_sentence_makes_the_day_unsure(line: str) -> None:
    """6차 리뷰: 띄어 쓴 "지난 해", "전년도", 년 없는 해, 날짜 뒤의 해 말을 못 봤다."""
    assert ("2026-11-13", False, False) in _all("입력 2026.11.10 09:30\n" + line)


def test_coming_and_shifted_years_are_read_from_the_day_written() -> None:
    assert ("2026-11-20", False, True) in _all("입력 2026.11.03\n오는 11월 20일 개최")
    assert ("2024-11-20", False, True) in _all("입력 2026.11.03\n재작년 11월 20일엔")
    assert ("2027-01-05", False, True) in _all("입력 2026.11.03\n이듬해 1월 5일 시행")


def test_an_end_weekday_that_would_put_the_end_before_the_start_drops_the_end() -> None:
    # 끝의 요일이 가리키는 해가 시작보다 앞이다 — 끝을 버린다(시작은 그대로)
    assert _days("11월 16일(월) ~ 11월 13일(금)") == [("2026-11-16", False)]
