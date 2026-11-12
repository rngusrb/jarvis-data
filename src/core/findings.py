"""스스로 알아본 것 — 탐색 루프가 남기고(brain/explore), 저장소가 두고(storage/findings), 신호와
맥락이 읽는다(triggers/findings, brain/providers). 셋이 같은 꼴을 쓰게 여기 둔다(JAR-046).

**날짜는 절대 날짜 칸에만 둔다.** 옛 생각 루프(2026-11-11 6차 BLOCKED 에서 멈춤)는 모델이 쓴 글을
저장해 두었다가 다음 날 다시 읽었고, 글 속 "내일·모레"가 틀렸다. 그걸 고치려던 코드가 맞는 글까지
망쳤다. 그래서 사실의 날짜는 `Fact.day` 에만 두고, 사람·모델에게 보일 때 코드가 그때의 오늘에 맞춰
"4일 뒤"를 센다 (`day_label`). 사용자에게 갈 말은 저장하지 않는다 — 말할 때 새로 쓴다.

그 날짜도 모델이 쓰지 않는다. 코드가 원자료(검색 결과·읽은 페이지·브라우저 화면)에서 뽑은 날짜표에서
모델은 번호만 고르고, 그 날짜가 적혀 있던 원문 줄을 같이 둔다(`Fact.quote`, brain/dates) — 탐색 2차
리뷰에서 기사 입력일·작년 날짜·찾기 모델이 바꿔 쓴 날짜가 "자료 어딘가에 있다"로 통과했다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Protocol, Tuple

from src.core.clock import WEEKDAYS

# 알아본 것의 값어치 — 탐색이 정하고, 신호(알릴 것만)·맥락(없음은 뺌)이 읽는다.
TELL, KEEP, NOTHING = "알릴 것", "기록만", "없음"
WORTHS = (TELL, KEEP, NOTHING)
# 알아본 것 신호의 줄기 — 게이트가 줄기 간격(하루 두 번)을 센다.
FAMILY = "finding"


def finding_trigger(finding_id: int) -> str:
    return f"{FAMILY}:{finding_id}"


@dataclass(frozen=True)
class Fact:
    """알게 된 사실 하나. 날짜는 절대 날짜로만(없으면 None) — 글에는 날짜 말을 쓰지 않는다.

    날짜는 코드가 원자료에서 뽑은 것이고(brain/dates), `quote` 는 그 날짜가 적혀 있던 원문 줄이다.
    모델이 날짜를 다른 사실에 잘못 붙였으면 보일 때 원문이 같이 보여 드러난다(JAR-046 2차 리뷰).
    """

    text: str
    day: Optional[date] = None
    source: str = ""
    quote: str = ""
    # 원문의 날짜는 있지만 해가 확실하지 않다 — `day` 는 비우고 원문만 보인다(거리를 세지
    # 않는다). 연도를 짐작하면 고칠 때마다 반대쪽으로 샜다(탐색 3·4차 리뷰) — 사용자 결정:
    # 확실할 때만 날짜로.
    no_year: bool = False


@dataclass(frozen=True)
class Finding:
    at: datetime
    # 씨앗의 갈래(관심사 · 못 한 답 · 자유 …)와 표 — 같은 씨앗을 얼마 안에 다시 뽑지 않으려고 센다.
    kind: str
    key: str
    subject: str
    # 알게 된 것의 제목 한 줄(날짜 없이 — 날짜 말이 있으면 탐색이 비운다)과 사실들. 다른 날 보일
    # 때는 이 둘만 보인다 — 모델이 쓴 긴 글은 저장하지 않는다.
    summary: str
    facts: Tuple[Fact, ...]
    # 읽은 곳(주소)과 쓴 검색어 — 같은 검색어를 며칠 안에 되풀이하지 않고, 하루 검색 수를 센다.
    sources: Tuple[str, ...]
    queries: Tuple[str, ...]
    worth: str
    # 알아보다 떠오른 "있으면 좋을 기능"(없으면 빈 글) — `/found` 가 모아 보인다.
    idea: str = ""
    # 제대로 판단하지 못한 까닭(시간 초과 · 검색 없이 씀 · 판단 꼴이 깨짐 …). 있으면 알리지 않는다.
    note: str = ""
    id: Optional[int] = None


class FindingSource(Protocol):
    """알아본 것을 읽는 쪽 — 맥락 묶음과 신호가 쓴다(구현은 storage/findings)."""

    def since(self, at: datetime) -> List[Finding]: ...


def day_label(day: date, today: date) -> str:
    """사실의 날짜를 오늘에 맞춰 — "11/16(월), 4일 뒤". 오늘·내일·모레·어제와 지난 것은 그 말로.

    코드가 센다. 모델에게 "4일 뒤"를 세게 하면 틀린다(생각 루프 1~6차 리뷰)."""
    shown = f"{day:%m/%d}({WEEKDAYS[day.weekday()]})"
    gap = (day - today).days
    named = {0: "오늘", 1: "내일", 2: "모레", -1: "어제"}
    if gap in named:
        return f"{shown}, {named[gap]}"
    if gap < 0:
        return f"{shown}, {-gap}일 지남"
    return f"{shown}, {gap}일 뒤"


def when_label(fact: Fact, today: date, quote: int = 120) -> str:
    """사실의 날짜 옆에 붙이는 말 — 코드가 센 거리와, 그 날짜가 적혀 있던 원문(`quote` 자까지).
    해를 모르는 날은 거리 없이 "해 모름"과 원문만. 날짜도 원문도 없으면 빈 글. 맥락·신호·`/found` 가
    같은 꼴로 보이게 여기 하나만 둔다."""
    if fact.day is not None:
        label = day_label(fact.day, today)
    elif fact.no_year and fact.quote:
        label = "해 모름"
    else:
        return ""
    if not fact.quote:
        return label
    shown = fact.quote if len(fact.quote) <= quote else fact.quote[:quote] + "…"
    return f'{label} — 원문 "{shown}"'
