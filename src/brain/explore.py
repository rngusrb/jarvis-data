"""무작위 탐색 — 시키지 않아도 거의 쉬지 않고 씨앗을 골라 찾아보고 쌓는다(JAR-046).

사용자 요청(2026-11-12): "무작위하게 항상 사용자가 뭘 관심 가지는지, 스스로 이런 게 있으면 좋지
않을까 하는 무작위성 스팟", "거의 항시 돌았으면 — 크롬 서치랑 인터넷 서치로 계속 여러 데이터
모으고".

한 번 = 씨앗 하나 → 찾기(검색·읽기·브라우저) → 판단(정해진 꼴) → 남김.

- **씨앗은 모델 없이, 무작위로** 고른다(싼 게이트 먼저). 먼저 갈래를 몫대로(관심사 6 · 못 한 답 2 ·
  자유 2), 그 안에서 씨앗을 무게대로 — 씨앗 수가 많은 갈래가 하루를 차지하지 않게(1차 리뷰: 본
  페이지 300개가 하루 96번 중 72%를 가져갔다). 같은 씨앗은 정해진 시간 안에 다시 뽑지 않는다.
- **찾기**는 도구 고리(brain/tools)를 그대로 쓴다. 검색 없이 쓴 글은 알아본 것이 아니다
  (research.py 와 같은 규칙). 같은 검색어는 며칠 안에 되풀이하지 않고(모델에게 보여 준다), 하루
  검색 수에 상한을 둔다 — 검색 엔진이 막는다.
- **날짜는 코드가 뽑는다**(brain/dates — 검색 결과·읽은 페이지·브라우저가 본 화면 그대로에서,
  찾기 모델이 쓴 글은 빼고). 판단은 그 날짜표에서 번호만 고른다(`when`). 해가 확실한 날만 날짜가
  되고, 아니면 원문만 남는다. 사실의 글엔 그 사실이 고른 날 말고는 날짜·요일·날짜 말을 두지 않는다
  — 있으면 그 사실을 버린다(고쳐 쓰지 않는다). 모델이 쓴 문장은 다른 날 다시 보이지 않는다 — 보일
  때는 사실 한 줄씩과 그 날짜의 원문, 코드가 센 거리만(core/findings 머리말).
- 실패(시간 초과·검색 없이 씀·판단 꼴이 깨짐)도 까닭과 함께 남긴다 — 같은 씨앗을 시계마다
  되풀이하지 않게. 두뇌에 닿지도 않으면 남기지 않고 루프에 올린다 — 씨앗을 태우지 않게.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from src.brain.client import Reasoner, unreachable
from src.brain.dates import Dated, dates_in
from src.brain.providers import TopicSource
from src.brain.tools import Researched, ToolLoop
from src.core.clock import KST, WEEKDAYS, local, shown, stamp
from src.core.findings import KEEP, NOTHING, TELL, WORTHS, Fact, Finding
from src.core.models import Trace
from src.core.topics import Trend, standings
from src.storage.findings import SQLiteFindings
from src.storage.selfchecks import SelfCheck

logger = logging.getLogger(__name__)

INTEREST, RETRY, FREE = "관심사", "못 한 답", "자유"
# 갈래 몫 — 먼저 갈래를 이 몫대로 고르고, 그 안에서 씨앗을 고른다. 고를 씨앗이 없는 갈래는 빠진다.
SHARES = {INTEREST: 6.0, RETRY: 2.0, FREE: 2.0}
# 제대로 판단하지 못한 까닭(`Finding.note`).
TIMEOUT, ERROR, UNSEARCHED, BROKEN = "시간 초과", "오류", "검색 없이 씀", "판단 꼴이 깨짐"
# 웹으로 다시 찾아볼 만한 막힘(자기 점검의 갈래). 로그인·내 기록·할 수 없는 일은 다시 찾아도 안
# 풀린다.
RETRYABLE = (
    "검색 결과가 모자람",
    "페이지를 못 읽음",
    "파일을 못 읽음",
    "브라우저로도 못 찾음",
    "도구 한도·시간",
    "있는 기능을 안 씀",
    "기타",
)
# 한 대화로 묶은 질문은 마지막 셋까지, 하나에 이만큼까지 씨앗에 보인다.
THREAD_SHOWN, QUESTION_CHARS = 3, 100
# 읽는 날에 기대는 날짜 말. 앞뒤가 이어진 이름("가나내일배움센터"·"다모레상회"·"오늘의집")은 뺀다.
# 이 말이 든 사실은 버리고, 제목에 있으면 제목을 비운다(1·2차 리뷰 — 오탐·미탐 예를 시험에 둔다).
# 자기 점검의 질문에 이 말이 있으면, 물어본 날이 지난 뒤엔 다시 찾지 않는다 — 그날에만 뜻이 있다
# (1차 리뷰: 11/12 22:00 의 "내일 오전 접수"를 11/13 에 다시 찾아 하루 밀린 날을 알렸다).
# "금주·차주"는 술을 끊음·차 주인과 같은 글자라 일정 말이 이어질 때만, "11월 20일 후"처럼 달이
# 앞에 있는 날(띄어 써도)은 절대 날짜다. "명일동"·"곧바로"는 빼고, "곧·임박·주말까지·N시간 뒤"는
# 넣는다(3차 리뷰). 다른 일에 기댄 기간("결제 후 30일 이내", "48시간 전 취소", "6개월 후 재응시")과
# "내일채움공제"는 날짜 말이 아니다 — 혜택 사실이 버려졌다(4차 리뷰).
_SOON = (
    r"(?=\s?(?:에|부터|까지|중|내|[월화수목금토일]요일"
    r"|마감|발표|공개|시작|종료|예정|공고|접수|진행))"
)
RELATIVE_WORDS = re.compile(
    r"(?<![가-힣])(?:오늘|내일|모레|어제|그제|그저께|글피|금일(?!봉)|익일|작일|명일(?!동|방주)"
    r"|이튿날)(?!배움|채움|저축|의집|로\s)"
    r"|(?<![가-힣])내달(?![리려렸])|(?<![가-힣])(?:금주|차주)"
    + _SOON
    + r"|이번\s?(?:주|달|[월화수목금토일]요일)|다음\s?(?:주|달)|지난\s?(?:주|달)"
    r"|오는\s?(?:[월화수목금토일]요일|\d{1,2}\s?일)|(?<![가-힣])이달|(?<![가-힣])내주(?:에|부터|까지|\s)"
    r"|(?<![가-힣])(?:곧(?![가-힣])|임박|주말까지|월말까지)"
    r"|(?:하루|이틀|사흘|나흘|닷새|엿새|열흘|일주일|보름)\s?(?:뒤|후|전|내|이내|남았|남음|남은|째)"
    r"|(?<![\d월])(?<!월\s)(?<!월\s\s)\d+\s?일\s?(?:뒤|후|전|남았|남음|남은|째)"
    r"|\d+\s?주일?\s?(?:뒤|후|전)|\d+\s?(?:시간|분)\s?(?:뒤|후)"
    r"|(?:한|두|세)\s?(?:주|달)\s?(?:뒤|후|전)"
    r"|D-\s?\d+"
)
# 바깥 검색으로 보내지 않을 것 — 전자우편·전화번호·주민번호 꼴. 씨앗에서 지운다(지시문 말고 코드로).
_PRIVATE = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+|(?<!\d)0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)|\d{6}-?[1-4]\d{6}"
)

LOOK = {
    INTEREST: (
        "사용자가 요즘 몰두하는 관심사다. 지금 그 사람에게 도움이 될 것 — 다가오는 일정·마감·"
        "발표, 새 소식, 쓸 만한 혜택·이벤트, 더 나은 방법 — 을 웹에서 찾는다."
    ),
    RETRY: (
        "사용자가 전에 물었는데 그때 제대로 답하지 못한 질문이다(막힌 곳과 함께). 지금 다시 "
        "찾아 그 질문의 답을 찾는다."
    ),
    FREE: (
        "사용자가 최근에 검색한 말 하나다. 거기서 더 알아 두면 그 사람에게 도움이 될 것을 웹에서 "
        "찾는다."
    ),
}
_RULES = """찾은 것을 다섯 줄 안으로 적는다. 줄마다 출처(사이트 이름).
일정·마감의 날짜는 자료에 적힌 그대로 옮긴다 — 바꿔 쓰거나 계산하지 않고,
글을 쓴 날(입력·작성일)은 적지 않는다.
찾을 만한 게 없으면 "없음" 한 단어만.
- 검색어에는 주제만 쓴다 — 이름·나이·소득·재산·주소·건강·연락처 같은 개인 사정은 빼고 일반화한다.
- [최근 검색어]에 있는 검색은 되풀이하지 않는다. 다른 각도로 찾는다.
- 지난 일정·오래된 소식은 쓰지 않는다."""

FACTS = 5  # 사실은 이만큼까지 — 늘어놓다 답이 잘렸다(서버 실측)
CHOICES = 30  # 날짜표 상한 — 가까운 날부터
JUDGE = f"""너는 개인 비서 "자비스"다. 스스로 알아본 것을 보고 정리한다. JSON 하나로만 답한다:
{{"title": "알게 된 것의 제목 한 줄 — 날짜 없이",
 "facts": [{{"text": "무엇인지 한 줄 — 날짜 없이", "when": [날짜표]의 번호 또는 null,
            "source": "사이트 이름"}}],
 "worth": "{TELL}" | "{KEEP}" | "{NOTHING}",
 "idea": "알아보다가 '자비스에 이런 기능이 있으면 좋겠다' 싶은 게 있으면 한 줄, 없으면 빈 글"}}

- facts 는 중요한 것부터 많아야 {FACTS}개.
- "{TELL}": 지금 알면 쓸모 있는 것 — 다가오는 마감·일정, 그 사람에게 맞는 혜택, 전에 못 찾은 답.
- "{KEEP}": 알아 두면 좋지만 급하지 않은 것.
- "{NOTHING}": 찾은 게 없거나("없음"), 지난 일. 이미 마감·종료됐다거나 못 찾았다는 것뿐이면
  "{TELL}" 이 아니다.
- 언제인지는 when 에 [날짜표]의 번호로만 둔다 — 그 줄의 원문이 바로 그 사실의 날짜를 말할 때만.
  글을 쓴 날, 다른 일의 날짜, "11월 중순"·"연말"처럼 날이 안 정해진 것은 null 이다.
- title·text·idea 에는 날짜도, 요일도, 오늘·내일·이번 주·며칠 뒤·임박·곧 같은 말도 쓰지 않는다 —
  다른 날 읽힌다. 쓰면 그 사실은 버려진다(날짜는 when 으로만).
- 지난 일은 넣지 않는다. 알아본 것에 없는 사실은 지어내지 않는다. 한국어로만."""

_LINE: Dict[str, Any] = {"type": "string", "maxLength": 160}
# 판단의 꼴 — 판단을 부르는 쪽(app/main)이 두뇌에 준다(`VLLMClient(schema=...)`). 지시문만으로는
# 값어치 칸에 제목을 쓰고, 사실을 늘어놓다 잘렸다(서버 실측 셋 중 둘). 꼴은 두뇌가 지키게 하고,
# `parse_judged` 는 꼴 없이 부른 두뇌에도 그대로 막는다. 칸 순서는 사실을 적은 뒤 값어치를 정하게.
JUDGED: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": _LINE,
        "facts": {
            "type": "array",
            "maxItems": FACTS,
            "items": {
                "type": "object",
                "properties": {
                    "text": _LINE,
                    "when": {
                        "anyOf": [
                            {"type": "integer", "minimum": 1, "maximum": CHOICES},
                            {"type": "null"},
                        ]
                    },
                    "source": {"type": "string", "maxLength": 60},
                },
                "required": ["text", "when", "source"],
                "additionalProperties": False,
            },
        },
        "worth": {"type": "string", "enum": list(WORTHS)},
        "idea": _LINE,
    },
    "required": ["title", "facts", "worth", "idea"],
    "additionalProperties": False,
}


class RecentTraces(Protocol):
    def recent(
        self, kind: str, since: datetime, limit: Optional[int] = None
    ) -> Sequence[Trace]: ...


class SelfChecks(Protocol):
    def since(self, at: datetime) -> List[SelfCheck]: ...


@dataclass(frozen=True)
class Seed:
    """씨앗 하나. `key` 로 같은 씨앗을 얼마 안에 다시 뽑지 않는다."""

    kind: str
    key: str
    subject: str
    detail: str = ""
    weight: float = 1.0


@dataclass(frozen=True)
class Judged:
    worth: str
    title: str
    facts: Tuple[Fact, ...]
    idea: str
    note: str = ""


class SearchDown(RuntimeError):
    """검색이 막혔다 — 씨앗 탓이 아니다. 남기지 않고 올려 루프가 물러선다(씨앗을 태우지 않게)."""


def _today_start(now: datetime) -> datetime:
    return datetime.combine(local(now).date(), time(0), tzinfo=KST)


def clean(text: str) -> str:
    """바깥 검색으로 갈 씨앗에서 전자우편·전화번호·주민번호 꼴을 지운다."""
    return _PRIVATE.sub("(지움)", text)


@dataclass
class Explorer:
    # 판단(낮은 온도로 부르는 쪽이 정한다)과 찾기(도구 고리 — 검색·읽기·브라우저).
    reasoner: Reasoner
    tools: ToolLoop
    findings: SQLiteFindings
    topics: Optional[TopicSource] = None
    checks: Optional[SelfChecks] = None
    traces: Optional[RecentTraces] = None
    # 씨앗을 고르는 무작위 — 시험에선 씨를 정한다.
    rng: random.Random = field(default_factory=random.Random)
    # 같은 관심사를 다시 알아보기까지. 12시간이면 오전에 바닥났다(1차 리뷰 모의). 본 검색은 한 번만.
    again: timedelta = timedelta(hours=6)
    # 못 한 답: 이 기간 안의 질문만, 묻고 이만큼 뒤부터, 하루 한 번, 이 횟수까지.
    retry_within: timedelta = timedelta(days=7)
    retry_after: timedelta = timedelta(hours=1)
    retries: int = 2
    # 이만큼 안에 이어 물은 질문은 한 대화로 묶는다 — "그 공고문 봐봐"·"그래서 봐 못 봐?" 같은
    # 뒷질문은 앞 질문 없이는 찾을 게 없다(서버 실측: 뒷질문이 혼자 씨앗이 되어 "없음").
    # 질문과 질문 사이로 잰다 — 찾아보는 답이 몇 분 걸리고 읽고 다시 묻는 데도 몇 분이다(2차 리뷰).
    thread_gap: timedelta = timedelta(minutes=20)
    # 자유 씨앗(최근 검색어)을 고를 기간과 후보 수. 검색 흔적의 종류와 앞말은 섹터의 것이라
    # 조립하는 곳(app/main)이 넘긴다 — 여기 박아 두면 섹터 쪽이 바뀔 때 자유 씨앗이 로그 없이
    # 사라진다(2차 리뷰).
    free_within: timedelta = timedelta(days=3)
    free_pool: int = 20
    search_kind: str = ""
    search_prefix: str = ""
    # 하루 검색 수 상한(검색 엔진 차단 방지)과 모델에게 보여 줄 최근 검색어 기간.
    daily_searches: int = 200
    query_memory: timedelta = timedelta(days=3)
    # 한 번에 찾는 시간 상한. 브라우저가 붙으면 그 상한(`ToolLoop.browse_seconds`)보다 길어야 한다 —
    # 짧으면 찾은 것과 검색어를 통째로 버린다(1차 리뷰).
    limit: timedelta = timedelta(minutes=3)
    browse_limit: timedelta = timedelta(minutes=6)

    def seeds(self, now: datetime) -> List[Seed]:
        """고를 수 있는 씨앗들(무게와 함께). 최근에 알아본 표는 뺀다."""
        recent = self.findings.explored(now - max(self.retry_within, timedelta(days=30)))
        out: List[Seed] = []
        out += [s for s in self._interests(now) if self._rested(recent.get(s.key), now, self.again)]
        out += [
            s
            for s in self._retries(now)
            if self._rested(recent.get(s.key), now, timedelta(days=1))
            and self.findings.tries(s.key) < self.retries
        ]
        out += [s for s in self._free(now) if s.key not in recent]
        return out

    @staticmethod
    def _rested(last: Optional[datetime], now: datetime, rest: timedelta) -> bool:
        return last is None or now - last >= rest

    def _interests(self, now: datetime) -> List[Seed]:
        if self.topics is None:
            return []
        board = [
            s
            for s in standings(self.topics.load(), self.topics.grounds(), now)
            if s.trend in (Trend.RISING, Trend.NEW, Trend.STEADY)
        ]
        paths = [tuple(s.path) for s in board]
        notes = self.topics.latest_notes(1)
        out = []
        for s in board:
            # 지켜보는 가지가 있는 줄기는 뺀다 — 검색어는 구체적일수록 낫다(research.interests 와
            # 같다).
            if any(len(p) > len(s.path) and p[: len(s.path)] == tuple(s.path) for p in paths):
                continue
            # 메모에는 쓴 날을 붙인다 — 메모 속 "이번 주"가 언제의 이번 주인지 알게(1차 리뷰).
            note = next(
                (f"메모({local(n.at):%m/%d}): {n.text}" for n in notes.get(s.node.id, [])), ""
            )
            out.append(
                Seed(
                    kind=INTEREST,
                    key=f"interest:{s.node.id}",
                    subject=clean(" > ".join(s.path)),
                    detail=clean(note),
                    weight=3.0 if s.trend in (Trend.RISING, Trend.NEW) else 1.0,
                )
            )
        return out

    def _retries(self, now: datetime) -> List[Seed]:
        """못 한 답 — 이어 물은 질문은 한 대화로 묶어 씨앗 하나로(앞 질문까지 보이게).

        대화가 "다 함"으로 끝났으면 풀린 것이다. 막힘이 웹으로 다시 찾아도 안 풀리는 것이어도 뺀다.
        """
        if self.checks is None:
            return []
        today = local(now).date()
        threads: List[List[SelfCheck]] = []
        for check in sorted(self.checks.since(now - self.retry_within), key=lambda c: c.at):
            if threads and check.at - threads[-1][-1].at <= self.thread_gap:
                threads[-1].append(check)
            else:
                threads.append([check])
        out = []
        for thread in threads:
            last = thread[-1]
            if last.result == "다 함" or last.blocked not in RETRYABLE:
                continue
            if now - last.at < self.retry_after:
                continue
            if local(thread[0].at).date() < today and any(
                RELATIVE_WORDS.search(c.question) for c in thread
            ):
                continue  # 그날에만 뜻이 있는 질문(위 RELATIVE_WORDS 설명)
            asked = thread[-THREAD_SHOWN:]
            stuck = [c for c in thread if c.result != "다 함"]
            blocked = ", ".join(dict.fromkeys(c.blocked for c in stuck))
            missing = " / ".join(dict.fromkeys(c.missing[:150] for c in stuck if c.missing))
            out.append(
                Seed(
                    kind=RETRY,
                    key=f"retry:{stamp(last.at)}",
                    subject=clean(" → ".join(c.question[:QUESTION_CHARS] for c in asked)),
                    detail=clean(
                        f"물어본 때: {shown(last.at)}. 그때 막힌 곳: {blocked}"
                        + (f" — {missing}" if missing else "")
                    ),
                )
            )
        return out

    def _free(self, now: datetime) -> List[Seed]:
        """최근 검색어 하나씩. 페이지 제목은 쓰지 않는다 — 메일·문서 제목처럼 개인 사정이 섞인다."""
        if self.traces is None or not self.search_kind or not self.search_prefix:
            return []
        unique: Dict[str, Trace] = {}
        for t in self.traces.recent(self.search_kind, now - self.free_within, limit=500):
            if not t.id or not t.text.startswith(self.search_prefix):
                continue
            words = t.text[len(self.search_prefix) :].strip()
            if not words or words.startswith("http") or _PRIVATE.search(words):
                continue
            unique.setdefault(words, t)
        picked = self.rng.sample(sorted(unique.items()), min(self.free_pool, len(unique)))
        return [
            Seed(kind=FREE, key=f"free:{t.id}", subject=words, detail=f"검색한 때: {shown(t.at)}")
            for words, t in picked
        ]

    def pick(self, seeds: Sequence[Seed]) -> Optional[Seed]:
        """갈래를 몫대로, 그 안에서 씨앗을 무게대로."""
        kinds = [k for k in SHARES if any(s.kind == k for s in seeds)]
        if not kinds:
            return None
        kind = self.rng.choices(kinds, weights=[SHARES[k] for k in kinds], k=1)[0]
        pool = [s for s in seeds if s.kind == kind]
        return self.rng.choices(pool, weights=[s.weight for s in pool], k=1)[0]

    def searched_today(self, now: datetime) -> int:
        return len(self.findings.queries_since(_today_start(now)))

    async def explore_once(self, now: datetime) -> Optional[Finding]:
        """씨앗 하나를 알아보고 남긴다. 고를 씨앗이 없거나 오늘 검색을 다 썼으면 None.

        두뇌에 닿지 않거나(`unreachable`) 검색이 막혔으면(`SearchDown`) 남기지 않고 올린다 — 루프가
        물러선다."""
        if self.searched_today(now) >= self.daily_searches:
            logger.info("탐색 — 오늘 검색 %d번을 다 썼다. 내일 다시", self.daily_searches)
            return None
        seed = self.pick(self.seeds(now))
        if seed is None:
            logger.info("탐색 — 고를 씨앗이 없다")
            return None
        recent = self.findings.queries_since(now - self.query_memory)
        limit = self.browse_limit if self.tools.browse is not None else self.limit
        try:
            found = await asyncio.wait_for(
                self.tools.run(self._ask(seed, recent, now)), timeout=limit.total_seconds()
            )
        except asyncio.TimeoutError:
            logger.warning("탐색 — %s 이 %d초를 넘겼다", seed.key, limit.total_seconds())
            return self._keep(seed, now, Judged(NOTHING, "", (), "", TIMEOUT))
        except Exception as error:
            if unreachable(error):
                raise
            logger.exception("탐색 — 찾다가 실패: %s", seed.key)
            return self._keep(seed, now, Judged(NOTHING, "", (), "", ERROR))

        queries = tuple(u.target for u in found.uses if u.kind == "search")
        sources = tuple(p.url for p in found.pages)
        text = found.text.strip()
        failed = [u for u in found.uses if u.kind == "search" and not u.ok]
        if not found.searched and failed:
            raise SearchDown(f"검색이 막혔다({failed[-1].note}) — {seed.key}")
        if not found.searched:
            judged = Judged(NOTHING, "", (), "", UNSEARCHED)
        elif not text or text.startswith("없음"):
            judged = Judged(NOTHING, "", (), "")
        else:
            judged = await self._judge(seed, found, now)
        return self._keep(seed, now, judged, queries, sources)

    def _ask(self, seed: Seed, recent: Sequence[str], now: datetime) -> List[Dict[str, Any]]:
        system = (
            f'너는 개인 비서 "자비스"다. 시키지 않았지만 스스로 알아본다. {LOOK[seed.kind]}\n'
            f"{_RULES}\n\n{self.tools.guide}"
        )
        memory = ", ".join(dict.fromkeys(reversed(recent))) if recent else "없음"
        user = (
            f"[지금] {shown(now)}\n[최근 검색어] {memory[:600]}\n\n"
            f"[씨앗 — {seed.kind}] {seed.subject}\n{seed.detail}".rstrip()
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    async def _judge(self, seed: Seed, found: Researched, now: datetime) -> Judged:
        today = local(now).date()
        # 날짜는 바깥에서 온 글 그대로에서만 — 찾기 모델이 쓴 글(`found.text`)은 빼고(2차 리뷰).
        choices = date_table(found.material, today)
        prompt = (
            f"[오늘] {today.isoformat()}({WEEKDAYS[today.weekday()]})\n"
            f"[씨앗 — {seed.kind}] {seed.subject}\n{seed.detail}\n\n"
            f"[알아본 것]\n{found.text.strip()}\n\n{render_table(choices)}"
        )
        try:
            raw = await self.reasoner.ask(prompt, system=JUDGE)
        except Exception as error:
            if unreachable(error):
                raise
            logger.exception("탐색 — 판단 실패: %s", seed.key)
            return Judged(NOTHING, "", (), "", ERROR)
        return parse_judged(raw, today, choices)

    def _keep(
        self,
        seed: Seed,
        now: datetime,
        judged: Judged,
        queries: Tuple[str, ...] = (),
        sources: Tuple[str, ...] = (),
    ) -> Finding:
        finding = Finding(
            at=now,
            kind=seed.kind,
            key=seed.key,
            subject=seed.subject,
            summary=judged.title,
            facts=judged.facts,
            sources=sources,
            queries=queries,
            worth=judged.worth,
            idea=judged.idea,
            note=judged.note,
        )
        saved = self.findings.write(finding)
        logger.info(
            "탐색 — %s %s → %s%s (사실 %d, 검색 %d, 읽기 %d)",
            seed.kind,
            seed.key,
            judged.worth,
            f" [{judged.note}]" if judged.note else "",
            len(judged.facts),
            len(queries),
            len(sources),
        )
        return replace(finding, id=saved)


# 오늘 날짜에 붙어야 할 일의 말. 글을 쓴 날은 오늘이나 지난 날뿐이다 — 지난 날은 날짜표에 안 들고,
# 오늘 날짜는 바이라인·머리의 날짜·글 쓴 시각 줄("2026. 11. 12. 9:30")일 때가 많다(3차 리뷰). 그래서
# 오늘 날짜는 같은 원문 줄에 일의 말이 있을 때만 받는다.
_EVENT = re.compile(
    r"마감|까지|접수|신청|모집|시험|발표|면접|행사|개최|시작|종료|오픈|진행|시행|공개|판매|할인"
    r"|이벤트|예약|출시|당일|일정|운영|공연|결과|개막|축제|설명회|열린다|발매|공지|선정|당첨|추첨"
)


def date_table(material: Sequence[str], today: date) -> List[Dated]:
    """원자료에서 코드가 뽑은 날짜 가운데 고를 수 있는 것 — 오늘 이후의 일의 날짜(글을 쓴 날
    빼고, 오늘 날짜는 일의 말이 있을 때만), 가까운 날부터 `CHOICES` 개. 원문 줄의 날짜 말은
    지운다."""
    found = [d for text in material for d in dates_in(text, today)]
    picks: Dict[Tuple[date, str], Dated] = {}
    # 해를 모르는 날은 자리값(오늘에 가장 가까운 해)이 지났으면 어느 해든 지난 날이다.
    counts = {"글 쓴 날": 0, "지난 날": 0, "오늘(일의 말 없음)": 0}
    for d in found:
        if d.written:
            counts["글 쓴 날"] += 1
        elif d.day < today:
            counts["지난 날"] += 1
        elif d.day == today and not _EVENT.search(d.line):
            counts["오늘(일의 말 없음)"] += 1
        else:
            # 원문 줄은 사실 옆에 7일 동안 보인다 — 그 안의 날짜 말("D-8", "오늘 마감")은 다른 날
            # 틀린다. 지우기만 한다(날짜로 바꿔 쓰지 않는다 — brain/_GUIDE 날짜 금지사항).
            line = RELATIVE_WORDS.sub("…", d.line)
            picks.setdefault((d.day, line), replace(d, line=line))
    choices = sorted(picks.values(), key=lambda d: d.day)[:CHOICES]
    logger.info(
        "탐색 — 날짜 %d개를 뽑아 %d개를 날짜표에(뺀 것: %s, 날짜표의 해 모름 %d)",
        len(found),
        len(choices),
        ", ".join(f"{k} {v}" for k, v in counts.items()),
        sum(1 for d in choices if not d.sure),
    )
    return choices


def render_table(choices: Sequence[Dated]) -> str:
    if not choices:
        return "[날짜표] 없음 — 코드가 자료에서 뽑은 오늘 이후의 날이 없다. when 은 모두 null."
    lines = [
        "[날짜표 — 자료(검색 결과·읽은 페이지·브라우저 화면)에 적힌 날짜를 코드가 뽑은 것."
        " 오늘 이후만. [해 모름]은 글에 해가 확실하지 않은 날 — 골라도 날짜는 안 붙고"
        " 원문만 남는다]"
    ]
    lines += [
        (
            f"{i}. {d.day.isoformat()}({WEEKDAYS[d.day.weekday()]})"
            if d.sure
            else f"{i}. {d.day:%m/%d} [해 모름]"
        )
        + f' — "{d.line}"'
        for i, d in enumerate(choices, 1)
    ]
    return "\n".join(lines)


_BARE_DAY = re.compile(
    r"(?<![\d월./])(\d{1,2})\s?일\s?(?=까지|마감|부터|오전|오후|\(|에\s|\d{1,2}시|\d{1,2}:"
    r"|발표|접수|시작|개강|오픈|종료|개최|공개|출시|예정)"
)
# 날 수를 세는 말 뒤의 "N일"은 날이 아니다("총 3일", "최대 7일").
_COUNTED = re.compile(r"(?:총|약|최대|최소|최장|단)\s*$")
# 해 모름 날의 사실 글엔 괄호 요일도 못 쓴다("서류 16일(월) 마감").
_WEEKDAY_PAREN = re.compile(r"\(\s*([월화수목금토일])(?:요일)?\s*\)")
_WEEKDAY_ONLY = re.compile(r"(?<![가-힣])([월화수목금토일])요일")
# 되풀이·운영 시간("매주 화·목요일 할인", "운영 월~금요일", "주말(토요일·일요일)", "수요일마다",
# "매월 15일까지") — 읽는 날에 기대지 않는다(5차 리뷰: 혜택 사실이 버려졌다).
_RECUR = re.compile(
    r"매주|격주|매월|매달|매년|마다|평일|주말|휴무|휴관|영업|운영"
    r"|[월화수목금토일]\s?(?:요일)?\s?[~∼·,、-]\s?[월화수목금토일]"
)
# 분수("반값(1/2)", "1/3 가격")는 날짜가 아니다.
_FRACTION = re.compile(
    r"\(\s*(?P<a>\d)\s?/\s?(?P<b>\d{1,2})\s*\)"
    r"|(?P<c>\d)\s?/\s?(?P<d>\d{1,2})(?=\s?(?:가격|값|할인|이상|이하|수준|정도|확률|만큼|분기))"
)
# 다른 일에 기댄 기간("구매확정 7일 후", "이용일 3일 전까지", "가입 후 24시간 후") — 앞이 조사
# 없는 이름말이면 그 일 기준이지 읽는 날 기준이 아니다. "발표는 3일 뒤", 문장 첫머리의 "3일 뒤"는
# 읽는 날 기준.
_SPAN = re.compile(r"\d+\s?(?:일|주일?|시간|분)\s?(?:뒤|후|전)")
_PARTICLE = re.compile(
    r"(?:[은는이가을를도만에서로]|약|대략|최소|최대|앞으로|이제|지금|부터|단|딱|불과|겨우|단지)$"
)


def _date_words(text: str) -> Optional[str]:
    """읽는 날에 기대는 날짜 말 — 다른 일에 기댄 기간("구매확정 7일 후")은 빼고."""
    for m in RELATIVE_WORDS.finditer(text):
        if _SPAN.fullmatch(m.group(0).strip()):
            ahead = text[: m.start()].rstrip()
            if ahead and re.search(r"[가-힣]$", ahead) and not _PARTICLE.search(ahead):
                continue
        return m.group(0)
    return None


def _stray(text: str, today: date, own: Optional[date] = None, own_sure: bool = True) -> str:
    """모델이 쓴 글의 날짜 문제 — 날짜 말이 있거나, 그 사실의 날(`own`, 날짜표에서 고른 것) 말고
    다른 날을 썼으면 그 까닭, 없으면 빈 글. 사용자 안: 날짜는 메타, 본문은 날짜 없이.

    해를 모르는 날을 골랐으면(`own_sure=False`) 글에도 해·요일을 쓰지 못한다 — 코드가 단정하지 않은
    해를 글이 단정한다(5차 리뷰). 달·날("11월 16일", "11/16")만 그 날과 맞으면 둔다."""
    word = _date_words(text)
    if word:
        return f"날짜 말 {word}"
    plain = _FRACTION.sub(_not_a_date, text)
    for d in dates_in(plain, today):
        if own is None:
            return f"제 날짜가 아닌 날 {d.day:%m/%d}"
        if d.sure and not own_sure:
            return f"해를 모르는 날에 해·요일 {d.day:%m/%d}"
        if (d.day.month, d.day.day) != (own.month, own.day) or (d.sure and d.day != own):
            return f"제 날짜가 아닌 날 {d.day:%m/%d}"
    # 달 없는 날("16일까지")·요일만("월요일 마감") — 그 사실이 고른 날과 맞을 때만(4차 리뷰). 되풀이
    # 하는 날("매월 15일", "매주 화요일")은 날짜 말이 아니다.
    for m in _BARE_DAY.finditer(plain):
        ahead = plain[: m.start()]
        if _RECUR.search(ahead[-4:]) or _COUNTED.search(ahead):
            continue
        if own is None or own.day != int(m.group(1)):
            return f"달 없는 날 {m.group(0).strip()}"
    # 요일은 그 요일 바로 곁에 되풀이·운영 말이 있을 때만 날짜 말이 아니다 — 문장 어딘가의 "운영"
    # 하나로 건너뛰면 "팝업스토어 금요일 오픈, 2주간 운영"이 남았다(6차 리뷰).
    for m in _WEEKDAY_ONLY.finditer(plain):
        if _RECUR.search(plain[max(0, m.start() - 8) : m.end() + 6]):
            continue
        if own is None or not own_sure or WEEKDAYS[own.weekday()] != m.group(1):
            return f"요일만 {m.group(0)}"
    for m in _WEEKDAY_PAREN.finditer(plain):
        if own is None or not own_sure or WEEKDAYS[own.weekday()] != m.group(1):
            return f"요일 {m.group(0)}"
    return ""


def _not_a_date(m: "re.Match[str]") -> str:
    """분수만 지운다 — 분자가 분모보다 작고 분모가 열 이하("(1/2)", "1/3 가격", "3/4분기").
    "(11/20)"은 날짜다."""
    top, bottom = (m.group("a"), m.group("b")) if m.group("a") else (m.group("c"), m.group("d"))
    return " " if int(top) < int(bottom) <= 10 else m.group(0)


def parse_judged(raw: str, today: date, choices: Sequence[Dated] = ()) -> Judged:
    """판단 JSON → `Judged`. 꼴이 깨지면 "없음"과 까닭(원문은 경고로).

    - 사실의 날짜는 날짜표(`choices`)의 번호로만 — 그 날짜와 원문 줄을 사실에 붙인다.
    - 사실의 글에 날짜 말이 있거나 고른 날 말고 다른 날을 썼으면 그 사실은 버린다. 제목·아이디어에
      날짜가 있으면 비운다. 고쳐 쓰지 않는다.
    - 남은 사실이 없으면 알리지 않는다(기록만). 사실은 `FACTS` 개까지.
    """
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start : end + 1]) if 0 <= start < end else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("worth") not in WORTHS:
        logger.warning("탐색 — 판단 꼴이 깨졌다: %.300s", raw)
        return Judged(NOTHING, "", (), "", BROKEN)
    items = data.get("facts") or []
    if not isinstance(items, list):
        logger.warning("탐색 — 사실 칸이 목록이 아니다: %.120s", items)
        items = []
    facts: List[Fact] = []
    for item in items:
        if len(facts) >= FACTS:
            break  # 꼴 없이 부른 두뇌가 늘어놓은 것 — 앞(중요한 것부터)만
        text = str(item.get("text") or "").strip() if isinstance(item, dict) else ""
        if not text:
            continue
        when = item.get("when")
        picked: Optional[Dated] = None
        if isinstance(when, int) and not isinstance(when, bool):
            if 1 <= when <= len(choices):
                picked = choices[when - 1]
            else:
                logger.info("탐색 — 날짜표에 없는 번호(%s), 날짜 없이: %.120s", when, text)
        elif when is not None:
            logger.info("탐색 — 번호가 아닌 when(%r), 날짜 없이: %.120s", when, text)
        why = _stray(text, today, picked.day if picked else None, picked.sure if picked else True)
        if why:
            logger.info("탐색 — %s이 든 사실을 버린다: %.120s", why, text)
            continue
        # 해가 확실하지 않은 줄을 골랐으면 날짜 없이 원문만 — 거리는 세지 않는다(사용자 결정).
        sure = picked is not None and picked.sure
        facts.append(
            Fact(
                text,
                picked.day if sure and picked else None,
                str(item.get("source") or "").strip(),
                picked.line if picked else "",
                no_year=picked is not None and not picked.sure,
            )
        )
    worth = str(data["worth"])
    title = str(data.get("title") or data.get("summary") or "").strip()
    if title and _stray(title, today):
        logger.info("탐색 — 제목을 비운다(%s): %.120s", _stray(title, today), title)
        title = ""
    idea = str(data.get("idea") or "").strip()
    if idea and _stray(idea, today):
        logger.info("탐색 — 아이디어를 비운다(%s): %.120s", _stray(idea, today), idea)
        idea = ""
    if worth == TELL and not facts:
        logger.info("탐색 — 남은 사실이 없어 알리지 않는다(기록만)")
        worth = KEEP
    return Judged(worth, title, tuple(facts), idea)
