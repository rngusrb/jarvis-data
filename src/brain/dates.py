"""자료에 적힌 날짜를 코드가 뽑는다 — 모델은 날짜를 쓰지 않고 고르기만 한다(JAR-046).

탐색의 판단이 날짜 칸을 채우게 했더니 기사 입력일(오늘)을 "11월 중순" 면접의 날로, 작년
"2025. 11. 16."을 올해로, 찾기 모델이 바꿔 쓴 "마감 2026-11-13"(원문은 "내일 마감")을 그대로
받았다(2차 리뷰 BLOCKED). 그래서 날짜는 **바깥에서 온 글**(검색 결과·읽은 페이지·브라우저가 본
화면)에서만 코드가 뽑는다. 모델은 뽑힌 것 가운데 번호를 고른다.

**해는 확실할 때만 정한다**(4차 리뷰 BLOCKED 뒤 사용자 결정). 연도 없는 날("11월 13일")의 해를
글 속 단서로 짐작했더니, 고칠 때마다 반대쪽으로 샜다 — 글에 적힌 해를 놓쳐 작년 일이 "내일"이
되고(3차), 그걸 고치니 작년 기사의 "올해"가 올해가 되고 "지난해보다" 한 마디에 올해 일정이
사라졌다(4차). 확실한 것은 셋뿐이다:

- 날짜에 해가 적혀 있다("2026.11.16", "26.11.16(월)", "26년 11월 16일").
- 요일이 해를 확인한다 — 기준일(그 글을 쓴 날, 모르면 오늘) 앞뒤에서 요일이 맞는 해. "11.16(일)"은
  2025년이다(2026-11-16 은 월요일). 맞는 해가 없으면 버린다(로그). 기간의 끝에만 요일이 있으면
  그 요일이 시작의 해도 정한다.
- 그 글을 쓴 날 — 그 줄이나 그 앞 줄의 바이라인(입력·송고·작성일, "가나일보 2026.11.03 09:30",
  검색 요약 맨 앞 "2025. 11. 3. —", 줄 맨 앞의 날짜+시각) — 이 있고, 날이 그 날 뒤 넉 달 안이거나 앞
  한 달 반 안일 때. 날짜 바로 앞의 "지난·오는·올해·작년·내년"도 그 날 기준으로만 읽는다. 같은
  문장에 그 날과 다른 해의 말("지난해 마감은 11월 13일")이 있으면 확실하지 않다.

그 밖의 해 언급("[2025 하반기]", "지난해보다", "2025년 1위")은 쓰지 않는다. 확실하지 않은 날은
`sure=False` — 거리를 세지 않고 원문 줄만 보인다(core/findings `Fact.no_year`).

- 글을 쓴 날(`written`)은 오늘이나 지난 날뿐이다 — 앞날은 늘 일의 날짜다. 뒤에 "까지·마감"이
  오거나 앞에 일의 말("서류 마감 2026.11.12 18:00")이 있거나 요일이 붙으면 일의 날짜다. 해가 적힌
  날짜+시각은 줄 어디에 있든 앞에 일의 말이 없으면 바이라인으로 본다(오늘의 "2026.11.12 14:00
  온라인 설명회"도 — BACKLOG). 기간의 끝은 늘 일의 날짜다.
- 기간: "~"·"부터"(사이에 시각이 있어도), 끝이 "20일"·"20(금)"·"11.20" 꼴. 줄표("-")는 끝에 달이나
  요일이 있을 때만 — "- 3일간", "- 1일 1회"는 기간이 아니다(5차 리뷰).
- 상대 날짜 말("내일 마감")·기간("11월 중순")은 뽑지 않는다. 주소 속 숫자도 보지 않는다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from src.core.clock import WEEKDAYS

logger = logging.getLogger(__name__)

# 날짜 옆에 남길 원문의 앞뒤 길이.
AROUND = 40
# 그 글을 쓴 날로 해를 정할 수 있는 거리 — 공고는 일보다 앞서 쓰고(넉 달), 지난 일을 적은 글은 조금
# 뒤에 쓴다(한 달 반). 그보다 멀면 어느 해인지 모른다(5차 리뷰: 1월 게시일의 11월 일정).
AHEAD, BEHIND = timedelta(days=120), timedelta(days=45)


@dataclass(frozen=True)
class Dated:
    """자료에서 뽑은 날짜 하나와 그 날짜가 든 원문."""

    # 해가 확실하지 않으면(`sure=False`) 오늘에 가장 가까운 해로 둔 자리값 — 거리를 세는 데 쓰지
    # 않는다(지난 날 거르기·월일 맞추기에만).
    day: date
    line: str
    # 글을 쓴 날(입력·작성일 등) — 일의 날짜가 아니다. 오늘·지난 날만.
    written: bool = False
    sure: bool = True
    # 요일 없는 "11/16" — 분수·점수("1/2", "3/5")와 헷갈린다.
    loose: bool = False


def _weekday(name: str) -> str:
    """ "(월)"·"(월요일)" 또는 띄어 쓴 "월요일"."""
    return (
        rf"(?:\s*\(\s*(?P<{name}>[{WEEKDAYS}])(?:요일)?\s*\)"
        rf"|\s+(?P<{name}x>[{WEEKDAYS}])요일)?"
    )


_DATE = re.compile(
    # 2026.11.16 · 2026-11-16 · 2026/11/16 · 2026년 11월 16일 · 2026. 11. 16.
    r"(?<![\d.])(?P<y1>20\d{2})\s*[.\-/년]\s*(?P<m1>\d{1,2})\s*[.\-/월]\s*(?P<d1>\d{1,2})(?![\d:])"
    r"\s*일?\.?"
    + _weekday("w1")
    # 26년 11월 16일
    + r"|(?<!\d)(?P<y2>\d{2})\s*년\s*(?P<m2>\d{1,2})\s*월\s*(?P<d2>\d{1,2})\s*일"
    + _weekday("w2")
    # 26.11.16(월) — 두 자리 해의 점 꼴은 판 번호와 헷갈려 요일이 붙을 때만
    + r"|(?<![\d.])(?P<y3>\d{2})\.\s?(?P<m3>\d{1,2})\.\s?(?P<d3>\d{1,2})\.?\s*"
    + rf"\(\s*(?P<w3>[{WEEKDAYS}])(?:요일)?\s*\)"
    # 11월 16일
    + r"|(?<!\d)(?P<m4>\d{1,2})\s*월\s*(?P<d4>\d{1,2})\s*일"
    + _weekday("w4")
    # 11/16 — 셈 말이 따로 붙으면 날짜가 아니다("4/5호선", "제12/13회", "11/16명"). "11/16개강"처럼
    # 일의 말이 붙은 건 날짜다.
    + r"|(?<![\d/.제B])(?P<m5>\d{1,2})\s?/\s?(?P<d5>\d{1,2})"
    r"(?![\d/%:]|층|호선|(?:호|회|명|점|개|위|권|번|차|등|배)(?![가-힣])|[FfPp](?![A-Za-z]))"
    + _weekday("w5")
    # 11.16(월) — 점 꼴은 소수("3.15%")와 헷갈려 요일이 붙을 때만
    + r"|(?<![\d.])(?P<m6>\d{1,2})\.\s?(?P<d6>\d{1,2})\.?\s*"
    + rf"\(\s*(?P<w6>[{WEEKDAYS}])(?:요일)?\s*\)"
    # 11.16~11.30 — 기간이면 요일 없이도(물결이 날짜임을 알려 준다)
    + r"|(?<![\d.])(?P<m7>\d{1,2})\.\s?(?P<d7>\d{1,2})\.?(?=\s*[~∼〜～]|-\d{1,2}\.\d{1,2})"
    # 12월 1~10일 — 첫날에 "일"이 없는 기간
    + r"|(?<!\d)(?P<m8>\d{1,2})\s*월\s*(?P<d8>\d{1,2})(?=\s*[~∼〜～]\s*\d{1,2}\s*일|-\d{1,2}일)"
)
_WEEKDAY_GROUPS = ("w1", "w1x", "w2", "w2x", "w3", "w4", "w4x", "w5", "w5x", "w6")
_TIME = r"(?:\s*(?:오전|오후)?\s*\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?|\s*\d{1,2}:\d{2})?"
# 기간의 끝 — 끝에 해("~2026.11.20", "~26.11.30(월)"), 달("~11.20"), 날만("~20일", "~20(금)",
# "~20,").
_TAIL = re.compile(
    _TIME + r"\s*(?P<sep>[~∼〜～–-]|부터)\s*"
    r"(?:(?P<y>(?:20)?\d{2})\s*[./년]\s*(?=\d{1,2}\s*[./월]\s*\d))?"
    r"(?:(?P<m>\d{1,2})\s*[./월]\s*(?P<d>\d{1,2})(?![\d:])\s*일?"
    r"|(?P<d2>\d{1,2})\s*(?:일(?!\s*(?:간|차|째|치|씩|분|동안|이내|내|간격|\d+\s*회))"
    r"|\.(?=\s*\()|(?=\s*\(|\s+[월화수목금토일]요일))"
    r"|(?P<d3>\d{1,2})(?=\s*(?:까지|,|\)|$)))\.?" + _weekday("w")
)
# 날짜 바로 앞의 말 — 그 글을 쓴 날을 알 때만 해로 읽는다("지난 11월 20일", "올해 11월 16일").
_YEAR_SHIFT = {
    "재작년": -2,
    "작년": -1,
    "지난해": -1,
    "전년": -1,
    "전년도": -1,
    "올해": 0,
    "금년": 0,
    "내년": 1,
    "이듬해": 1,
}
_MARK = re.compile(
    r"(?<![가-힣])(지난|오는|재작년|작년|지난해|올해|금년|내년|이듬해)"
    r"(?:엔|에는|에|은|는|의)?\s*$"
)
# 같은 문장의 해 말 — 그 글을 쓴 날과 다른 해면 그 문장의 날은 확실하지 않다.
_YEAR_WORD = re.compile(
    r"(?<![가-힣])(?P<rel>재작년|작년|지난\s?해|전년도?|올해|금년|내년|이듬해)"
    r"|(?<!\d)(?P<y>(?:19|20)\d{2})(?=\s*(?:년|학년도|상반기|하반기|시즌|[.\-/]\s*\d)|\s*[\]\)])"
)
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")
_URL = re.compile(r"https?://\S+|www\.\S+")
_HOST = re.compile(r"[\w.-]*\.[A-Za-z]{2,}")
# 글을 쓴 날의 이름표. "입력·송고·승인"은 그것만으로, "작성·등록·게시"는 "작성일·등록일"처럼 날을
# 뜻하거나 쌍점·시각이 붙을 때만 — "정보 입력 기간"·"등록 마감"·"합격자 공고 11/12"는 일의 날짜다.
_STRONG = r"(?:기사\s?입력|최종\s?수정|입력|송고|승인)"
_WEAK = r"(?:작성|등록|수정|게시|게재|발행|업데이트|공고)"
_LABELED = re.compile(rf"(?:{_STRONG}|{_WEAK})\s*(?:일자|일시|날짜|일)\s*[:：]?\s*$")
_BARE_STRONG = re.compile(rf"{_STRONG}\s*[:：]?\s*$")
_BARE_WEAK = re.compile(rf"{_WEAK}\s*[:：]\s*$")
_AFTER = re.compile(rf"^\s*(?:\d{{1,2}}:\d{{2}}(?::\d{{2}})?\s*)?{_STRONG}(?![가-힣])")
_TIME_AFTER = re.compile(r"^\s*\d{1,2}:\d{2}(?!\d)")
# 앞에 이런 말이 있으면 그 날짜+시각은 바이라인이 아니라 일의 날짜다("서류 마감 2026.11.12 18:00").
_TASK_BEFORE = re.compile(
    r"마감|접수|시작|종료|발표|시험|기간|일시|일정|행사|개최|설명회|면접|오픈|공연|신청|모집|운영"
    r"|진행|합격|결과|최종|예정|까지"
)
# 뒤에 "까지·마감"이 오면 일의 날짜다.
_DEADLINE = re.compile(r"^\s*(?:\d{1,2}:\d{2}\s*)?(?:까지|마감)")
# 검색 요약 맨 앞의 날짜("2026. 11. 12. — 가나은행이 …")는 그 글을 쓴 날이다.
_LEAD = re.compile(r"^\s*(?:[—–]|\.\.\.|…)")


def _valid(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _around(month: int, day: int, ref: date) -> List[date]:
    return [d for y in (ref.year - 1, ref.year, ref.year + 1) if (d := _valid(y, month, day))]


def _nearest(month: int, day: int, ref: date) -> Optional[date]:
    options = _around(month, day, ref)
    return min(options, key=lambda d: abs((d - ref).days)) if options else None


def _next(month: int, day: int, ref: date) -> Optional[date]:
    return min((d for d in _around(month, day, ref) if d >= ref), default=None)


def _previous(month: int, day: int, ref: date) -> Optional[date]:
    return max((d for d in _around(month, day, ref) if d <= ref), default=None)


def _strip_urls(line: str) -> str:
    """주소를 지운다 — 스킴이 없는 "news.example.com/2026/11/12/" 도. 토막마다 본다(긴 줄에서도
    느려지지 않게). "2026.11.16/18:00"은 주소가 아니다(끝이 글자 도메인이 아니다)."""
    line = _URL.sub(" ", line)
    if "/" not in line:
        return line
    words = line.split(" ")
    for i, word in enumerate(words):
        host = word.split("/", 1)[0]
        if "/" in word and "." in host and _HOST.fullmatch(host):
            words[i] = " " * len(word)
    return " ".join(words)


def _quote(line: str, start: int, end: int) -> str:
    left = max(0, start - AROUND)
    right = min(len(line), end + AROUND)
    text = " ".join(line[left:right].split())
    return ("…" if left > 0 else "") + text + ("…" if right < len(line) else "")


def _weekday_of(m: "re.Match[str]", groups: Tuple[str, ...] = _WEEKDAY_GROUPS) -> Optional[str]:
    return next((m.group(g) for g in groups if m.group(g)), None)


def _is_written(
    line: str, m: "re.Match[str]", day: date, today: date, full: bool, weekday: bool
) -> bool:
    """그 날짜가 글을 쓴 날인가 — 오늘·지난 날만, 뒤에 "까지·마감"이 오면 아니다."""
    if day > today:
        return False
    before, after = line[: m.start()], line[m.end() :]
    if _DEADLINE.match(after):
        return False
    if _LABELED.search(before[-16:]):
        return True
    if full and (_BARE_STRONG.search(before[-12:]) or _AFTER.match(after)):
        return True
    if weekday:
        return False  # 요일을 붙인 날은 일의 날짜다("합격자 공고: 2026.11.12(목)")
    if _BARE_WEAK.search(before[-12:]):
        return True
    if not before.strip() and _LEAD.match(after):
        return True
    # 해가 적힌 날짜+시각인데 앞에 일의 말이 없다 — 바이라인("가나일보 2026.11.03 09:30", 줄 맨 앞의
    # "2026. 11. 12. 9:30")
    return full and bool(_TIME_AFTER.match(after)) and not _TASK_BEFORE.search(before)


def _full_year(m: "re.Match[str]") -> Optional[int]:
    if m.group("y1"):
        return int(m.group("y1"))
    if m.group("y2"):
        return 2000 + int(m.group("y2"))
    if m.group("y3"):
        return 2000 + int(m.group("y3"))
    return None


def _month_day(m: "re.Match[str]") -> Tuple[int, int]:
    for mm, dd in (
        ("m1", "d1"),
        ("m2", "d2"),
        ("m3", "d3"),
        ("m4", "d4"),
        ("m5", "d5"),
        ("m6", "d6"),
        ("m7", "d7"),
    ):
        if m.group(mm):
            return int(m.group(mm)), int(m.group(dd))
    return int(m.group("m8")), int(m.group("d8"))


def _stamps(lines: List[str], today: date) -> List[Optional[date]]:
    """줄마다 그 글을 쓴 날.

    - 그 줄에 바이라인이 있으면 그 줄의 가장 늦은 것("등록일 2025.10.01 수정일 2026.10.15").
    - 없으면 앞 줄들의 마지막 바이라인 — 단, 뒤 줄들의 첫 바이라인과 해가 같을 때만. 목록에서 날짜
      줄이 항목 뒤에 오면 앞 바이라인은 앞 항목의 것이다(6차 리뷰: 작년 공고가 확실한 "내일"이
      됐다). 기사 본문은 위의 바이라인을 따르되, 아래 댓글의 해가 다르면 어느 쪽인지 모른다.
    """
    own: List[Optional[date]] = []
    for line in lines:
        found: Optional[date] = None
        for m in _DATE.finditer(line):
            year = _full_year(m)
            if year is None:
                continue
            month, dd = _month_day(m)
            day = _valid(year, month, dd)
            weekday = _weekday_of(m) is not None
            if day is not None and _is_written(line, m, day, today, True, weekday):
                found = day if found is None else max(found, day)
        own.append(found)
    out: List[Optional[date]] = []
    for i, mine in enumerate(own):
        if mine is not None:
            out.append(mine)
            continue
        before = next((d for d in reversed(own[:i]) if d is not None), None)
        after = next((d for d in own[i + 1 :] if d is not None), None)
        out.append(
            before if before is not None and (after is None or after.year == before.year) else None
        )
    return out


def _other_year(before: str, after: str, stamp: date) -> bool:
    """같은 문장에 그 글을 쓴 날과 다른 해의 말이 있나 — 날짜 앞뒤 모두("지난해 서류 마감일은 11월
    13일이었다", "11월 13일이었던 지난해와 달리", "[2025 하반기] … 11월 13일")."""
    sentence = _SENTENCE_END.split(before[-200:])[-1] + " " + _SENTENCE_END.split(after[:200])[0]
    for m in _YEAR_WORD.finditer(sentence):
        if m.group("y"):
            year = int(m.group("y"))
        else:
            year = stamp.year + _YEAR_SHIFT[re.sub(r"\s", "", m.group("rel"))]
        if year != stamp.year:
            return True
    return False


def _resolve(
    month: int,
    dd: int,
    weekday: Optional[str],
    before: str,
    stamp: Optional[date],
    today: date,
    after: str = "",
) -> Tuple[Optional[date], bool]:
    """연도 없는 날짜 — (날, 해가 확실한가). 확실하지 않으면 오늘에 가장 가까운 해의 자리값."""
    if weekday:
        ref = stamp or today
        # 쓴 날을 모르면 오늘 뒤로는 반년까지만 — 옛 글의 "11월 16일(화)"가 1년 뒤로 가지 않게.
        late = ref + timedelta(days=400 if stamp else 200)
        options = [
            d
            for d in _around(month, dd, ref)
            if WEEKDAYS[d.weekday()] == weekday and ref - timedelta(days=400) <= d <= late
        ]
        if not options:
            logger.info("날짜 — 요일이 맞는 해가 없어 버린다: %d/%d(%s)", month, dd, weekday)
            return None, False
        return min(options, key=lambda d: abs((d - ref).days)), True
    placeholder = _nearest(month, dd, today)
    if placeholder is None:
        return None, False
    mark = _MARK.search(before[-8:])
    word = mark.group(1) if mark else None
    if word == "지난":
        # 지난 날이다 — 쓴 날을 모르면 오늘 이전의 그 날(어차피 지난 날이라 날짜표에 안 든다)
        return _previous(month, dd, stamp or today), stamp is not None
    if stamp is None:
        return placeholder, False  # 그 글을 쓴 날을 모른다 — 해를 모른다
    if word == "오는":
        return _next(month, dd, stamp), True
    if word is not None:
        return _valid(stamp.year + _YEAR_SHIFT[word], month, dd), True
    if _other_year(before, after, stamp):
        return placeholder, False
    ahead = _next(month, dd, stamp)
    if ahead is not None and ahead - stamp <= AHEAD:
        return ahead, True
    back = _previous(month, dd, stamp)
    if back is not None and stamp - back <= BEHIND:
        return back, True
    return placeholder, False


def _tail_end(
    start: date, tail: "re.Match[str]", stamp: Optional[date], today: date
) -> Tuple[Optional[date], bool]:
    """기간의 끝과, 끝의 요일·해가 해를 확인했나. 끝에 해·요일이 없으면 시작과 같은 해(달 없는 끝이
    시작보다 앞이면 다음 달, 달이 있는데 앞이면 다음 해)."""
    weekday = tail.group("w") or tail.group("wx")
    if tail.group("m"):
        month, dd = int(tail.group("m")), int(tail.group("d"))
        if tail.group("y"):
            year = int(tail.group("y"))
            end = _valid(year if year >= 2000 else 2000 + year, month, dd)
            if end is not None and weekday and WEEKDAYS[end.weekday()] != weekday:
                return None, False
            return end, end is not None
    else:
        dd = int(tail.group("d2") or tail.group("d3"))
        month = start.month
        if dd < start.day:
            month = 1 if start.month == 12 else start.month + 1
    if weekday:
        end, _ = _resolve(month, dd, weekday, "", stamp, today)
        if end is not None and end < start:
            end = None  # 끝이 시작보다 앞이 된다 — 요일이 시작과 다른 해를 가리킨다
        if end is None:
            logger.info("날짜 — 기간 끝의 요일이 안 맞아 끝을 버린다: %s", tail.group(0).strip())
        return end, end is not None
    end = _valid(start.year, month, dd)
    if end is not None and end < start:
        end = _valid(start.year + 1, month, dd)
    return end, False


def dates_in(text: str, today: date) -> List[Dated]:
    """글에 적힌 날짜들, 나온 순서대로. 하나의 글(검색 결과 하나·페이지 하나·화면 하나)씩 부른다 —
    연도 없는 날짜는 그 글을 쓴 날을 기준으로 본다."""
    lines = [_strip_urls(raw) for raw in text.splitlines()]
    stamps = _stamps(lines, today)
    out: List[Dated] = []
    # 한 줄에 같은 날은 한 번 — 해가 확실한 쪽, 요일 있는 쪽을 남긴다.
    kept: Dict[Tuple[int, date, bool], int] = {}

    def add(number: int, dated: Dated) -> None:
        key = (number, dated.day, dated.written)
        if key not in kept:
            kept[key] = len(out)
            out.append(dated)
        elif (dated.sure, not dated.loose) > (out[kept[key]].sure, not out[kept[key]].loose):
            out[kept[key]] = dated

    for number, line in enumerate(lines):
        stamp = stamps[number]
        consumed = 0  # 기간의 끝으로 이미 읽은 곳 — 다시 따로 잡지 않는다
        for m in _DATE.finditer(line):
            if m.start() < consumed:
                continue
            weekday = _weekday_of(m)
            month, dd = _month_day(m)
            year = _full_year(m)
            day: Optional[date]
            if year is not None:
                day, sure = _valid(year, month, dd), True
                if day is not None and weekday and WEEKDAYS[day.weekday()] != weekday:
                    logger.info("날짜 — 적힌 해와 요일이 어긋나 버린다: %s", m.group(0).strip())
                    day = None
            else:
                day, sure = _resolve(
                    month, dd, weekday, line[: m.start()], stamp, today, line[m.end() :]
                )
            if day is None:
                continue
            loose = bool(m.group("m5")) and weekday is None
            end: Optional[Dated] = None
            tail = _TAIL.match(line, m.end())
            if tail is not None and _is_span(tail):
                consumed = tail.end()
                last, last_sure = _tail_end(day, tail, stamp, today)
                if last is not None:
                    if last_sure and not sure:
                        # 끝의 요일·해가 해를 확인했다 — 시작도 같은 기간이라 그 해(넘기면 앞 해)
                        year_of_start = (
                            last.year if (month, dd) <= (last.month, last.day) else (last.year - 1)
                        )
                        fixed = _valid(year_of_start, month, dd)
                        if fixed is not None:
                            day, sure = fixed, True
                    if last != day:
                        end = Dated(
                            last, _quote(line, m.start(), consumed), False, sure or last_sure, loose
                        )
            written = _is_written(line, m, day, today, year is not None, weekday is not None)
            add(number, Dated(day, _quote(line, m.start(), m.end()), written, sure, loose))
            if end is not None:
                add(number, end)
    return out


def _is_span(tail: "re.Match[str]") -> bool:
    """줄표("-")는 끝에 달이나 요일이 있을 때만 기간이다 — "- 3일간 진행", "- 1일 1회 응모"는
    아니다. 날만 있는 끝("~20,")은 물결 뒤에서만."""
    sep = tail.group("sep")
    glued = tail.group(0).lstrip().startswith(sep) and not re.search(r"\s", tail.group(0).strip())
    if sep in ("-", "–") and not tail.group("m") and not (tail.group("w") or tail.group("wx")):
        # 붙여 쓴 "16-20일"만 기간이다 — 띄어 쓴 "- 3일"은 아니다
        return glued and bool(tail.group("d2"))
    return not (tail.group("d3") and sep not in ("~", "∼", "〜", "～"))
