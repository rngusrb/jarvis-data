"""쉬는 날 — 주말과 공휴일.

평일만 "평소"로 치는 섹터(이동)가 있다 — 주말 기록 하나가 평균을 몇 시간씩 끌어내린다
(commute/_GUIDE). 공휴일도 같다: 추석 연휴의 늦은 아침을 "평소보다 늦게 나섬"으로 읽으면 안 된다.

공휴일 표는 바깥의 사실이다(나라가 정한다). 받는 일은 섹터(날씨 — 특일 정보)가, 담는 일은 저장소
(`storage/days.py`)가 하고, 여기는 "그날 쉬나"만 답한다. 표는 프로세스에 하나다 — 트리거가 순수
함수라 인자로 받을 길이 없어서, 시작할 때와 새로 받을 때 `set_holidays` 로 채운다. 시험도 같은
함수로 바꾼다. 표가 비어 있으면 주말만 쉬는 날이다(받기 전과 같다 — 받는 쪽이 실패를 로그에 남긴다).
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Mapping, Optional

_HOLIDAYS: Dict[date, str] = {}


def set_holidays(days: Mapping[date, str]) -> None:
    """공휴일 표를 통째로 바꾼다(날짜 → 이름)."""
    _HOLIDAYS.clear()
    _HOLIDAYS.update(days)


def holiday(day: date) -> Optional[str]:
    """그날이 공휴일이면 이름("추석"), 아니면 None."""
    return _HOLIDAYS.get(day)


def is_workday(day: date) -> bool:
    """평일이고 공휴일이 아닌 날."""
    return day.weekday() < 5 and day not in _HOLIDAYS
