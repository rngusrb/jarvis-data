"""실제 맥락 제공자들.

지금은 이미 손에 있는 정보(관측치 추이와 최근 값, 최근 발화, 수집 현황, 흔적 하루 요약, 주제 나무,
최근 사건)만 쓴다. 반응·대화·보고서가 같은 제공자를 창 크기만 달리해 쓴다 — 보고서 쪽 조립은
`app/main.py` 의 `build_reporter`. 캘린더 파서와 저장소가 생기면 ScheduleProvider 같은 게 여기 늘어난다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from src.brain.context import ContextBlock
from src.brain.memory import SpeechLog
from src.core.clock import KST, WEEKDAYS, local
from src.core.events import EventKind, EventSource
from src.core.metrics import Metric
from src.core.models import Insight, ObservationCatalog, ObservationSource, Trace
from src.core.topics import RECENT_DAYS, Ground, Tree, Trend, standings
from src.core.traces import TraceKind
from src.storage.topics import Note


@dataclass
class ObservationTrendProvider:
    """신호에 딸려온 관측치를 추이로 펼쳐준다.

    요약문("평균보다 2시간 짧음")만 주면 모델이 판단할 근거가 얇다.
    실제 숫자 흐름을 보여주면 "3일째 계속 줄고 있네" 같은 말을 할 수 있게 된다.
    """

    name: str = "observation_trend"
    max_points: int = 7

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        # 신호에 딸린 것만 본다. 사용자가 먼저 말을 걸었을 때는 줄 게 없고,
        # 그때 필요한 "최근 값"은 RecentValuesProvider 가 맡는다.
        if insight is None or len(insight.observations) < 2:
            return None
        recent = sorted(insight.observations, key=lambda o: o.at)[-self.max_points :]
        kind = recent[0].kind
        lines = [f"- {local(o.at):%m/%d} {o.value:g}" for o in recent]
        return ContextBlock(label=f"{kind} 최근 추이", body="\n".join(lines))


class Collected(Protocol):
    """수집 경로 현황에 올라가는 카드 — 지표·흔적·사건 카드가 모두 이 모양이다."""

    @property
    def kind(self) -> str: ...

    @property
    def label(self) -> str: ...

    @property
    def collector(self) -> Optional[str]: ...

    @property
    def stale_after(self) -> timedelta: ...

    @property
    def active(self) -> bool: ...


@dataclass
class SpeechHistoryProvider:
    """최근에 자비스가 뭐라고 했는지 알려준다.

    게이트(쿨다운)가 같은 트리거의 재발화를 막는다면, 이건 *다른* 트리거끼리
    비슷한 말을 반복하는 걸 막는다. 수면 얘기 한 지 두 시간 만에
    "피곤해 보여요"라고 또 하면 사용자는 알림을 끈다.
    """

    log: SpeechLog
    name: str = "speech_history"
    window: timedelta = timedelta(days=1)

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        records = self.log.since(now - self.window)
        if not records:
            return None
        lines = [f"- {local(r.at):%m/%d %H:%M} ({r.trigger}) {r.text}" for r in records]
        return ContextBlock(label="최근 24시간 동안 내가 한 말", body="\n".join(lines))


@dataclass
class CollectionStatusProvider:
    """자비스가 **자기 수집 구조**를 알게 한다.

    이게 없으면 모델은 "걸음수가 며칠째 없다"만 보고 그럴듯한 일반론을 지어낸다.
    실제로 "배터리 최적화를 해제하라"고 답한 적이 있는데, 안드로이드 개념이고
    이 시스템엔 존재하지도 않는 이야기다. 원인은 그냥 걸음수 수집기를 아직
    안 만든 것이었다.

    수집 **방식**은 선언으로 받고(코드가 알 수 없는 설정 사실), 살아 있는지는
    데이터에서 판단한다(선언만 믿으면 낡는다).
    """

    catalog: ObservationCatalog
    # 지표·흔적·사건 카드. 지표만 보면 다른 표로 옮긴 종류(위치 → 사건)가 "수집기 없음"이
    # 되거나 목록에서 사라져, 자비스가 위치를 모으고 있다는 것 자체를 모른다.
    cards: Sequence[Collected]
    name: str = "collection_status"

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        last_seen = self.catalog.last_seen()
        known = {m.kind: m for m in self.cards}
        lines: List[str] = []

        for kind in sorted(set(known) | set(last_seen)):
            metric = known.get(kind)
            at = last_seen.get(kind)

            if metric is None:
                state = "수집기 없음 — 아직 만들지 않았다"
            elif not metric.active:
                # 안 만든 것과 일부러 버린 것은 다르다. 섞으면 자비스가
                # 버린 지표를 되살리라고 조른다.
                if at is not None and now - at <= metric.stale_after:
                    # 접었다고 선언해놓고 데이터는 계속 들어오는 상태.
                    # 선언과 현실이 어긋났으니 그대로 말한다.
                    state = "수집 중단했다고 선언됐는데 데이터는 계속 들어온다 — 선언이 낡았다"
                else:
                    state = "수집 중단 — 과거 데이터만 있다. 되살릴 필요 없다"
            elif at is None:
                state = f"{metric.collector} (아직 한 번도 안 들어옴)"
            elif now - at > metric.stale_after:
                hours = (now - at).total_seconds() / 3600
                state = f"{metric.collector} — 그런데 {hours:.0f}시간째 끊김"
            else:
                state = f"{metric.collector} — 정상"

            name = f"{metric.label}({kind})" if metric else kind
            lines.append(f"- {name}: {state}")

        return ContextBlock(label="수집 경로 현황", body="\n".join(lines))


@dataclass
class RecentValuesProvider:
    """지표별 **최근 값을 직접 조회해서** 보여준다.

    `ObservationTrendProvider` 와 재료가 다르다. 저쪽은 신호에 딸려온 것만
    보고, 이쪽은 저장소를 직접 읽는다.

    사고 이력: 2026-10-02. 신호에 딸린 것만 보는 제공자뿐이라, 사용자가
    "데이터 보고 말해봐"라고 세 번 물었는데 자비스가 세 번 다 "값이 없어서
    판단할 수 없어"라고 답했다. 수면도 심박도 위치도 멀쩡히 쌓여 있었다 —
    **프롬프트에 안 들어갔을 뿐이다.**

    접힌(retired) 지표는 뺀다. 과거 데이터만 남은 것을 현재 상태인 양
    보여주면 "걸음수가 646보네"라고 두 달 전 숫자를 읽는다.
    """

    source: ObservationSource
    metrics: Sequence[Metric]
    name: str = "recent_values"
    window: timedelta = timedelta(days=14)
    max_points: int = 7

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        chunks: List[str] = []
        for metric in self.metrics:
            if not metric.active:
                continue
            rows = sorted(self.source.recent(metric.kind, now - self.window), key=lambda o: o.at)
            if not rows:
                continue
            recent = rows[-self.max_points :]
            values = " · ".join(f"{local(o.at):%m/%d} {o.value:g}" for o in recent)
            chunks.append(f"- {metric.label}({metric.kind}): {values}")

        if not chunks:
            return None
        # 몇 개까지 보이는지도 이름에 적는다 — "최근 90일 값"에 14개만 나오면 석 달 중 14일만 기록된
        # 것으로 읽힌다(JAR-027 리뷰 재현).
        label = f"최근 {self.window.days}일 값(지표마다 최근 {self.max_points}개까지)"
        return ContextBlock(label=label, body="\n".join(chunks))


class RecentTraces(Protocol):
    def recent(
        self, kind: str, since: datetime, limit: Optional[int] = None
    ) -> Sequence[Trace]: ...


@dataclass
class TraceDigestProvider:
    """하루 요약이 있는 흔적 종류(앱 사용, 웹 방문)를 어제·오늘치로 보여준다.

    흔적은 낱줄로는 맥락에 안 들어간다 — 하루 수백 줄이다. 카드가 들고 온 요약으로 접어서
    넣는다. "어젯밤 늦게 잠들었다"는 신호 옆에 "새벽 두 시까지 맥을 썼다"가 있어야 말이 된다.

    민감한 종류(웹 방문의 검색어)는 `include_sensitive` 일 때만 넣는다. 반응·대화의 말은
    텔레그램으로 나가는데, 거기서 민감한 걸 거르는 장치는 아직 없다(4단계). 넣을지는 조립하는
    곳이 정한다.
    """

    source: RecentTraces
    kinds: Sequence[TraceKind]
    name: str = "trace_digest"
    # 어제 하루와 오늘 지금까지. 요약이 창 앞에서 잘린 날을 빼서 두 줄 남짓이 된다.
    window: timedelta = timedelta(days=2)
    include_sensitive: bool = False

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        since = now - self.window
        chunks: List[str] = []
        for card in self.kinds:
            if card.digest is None or not card.active:
                continue
            if card.sensitive and not self.include_sensitive:
                continue
            days = card.digest(self.source.recent(card.kind, since), since, now)
            if days:
                # 읽는 법은 요약을 만드는 섹터가 적는다(하루가 언제 바뀌나 등).
                how = f" — 읽는 법: {card.reading}" if card.reading else ""
                chunks.append(f"- {card.label}{how}:\n" + "\n".join(f"  {day}" for day in days))

        if not chunks:
            return None
        return ContextBlock(label="최근 하루 요약", body="\n".join(chunks))


class TopicSource(Protocol):
    def load(self) -> Tree: ...

    def grounds(self) -> List[Ground]: ...

    def latest_notes(self, per_topic: int) -> Dict[Optional[int], List[Note]]: ...


# 맥락에 넣는 주제. 처음 보인 것(근거가 하루뿐)은 옛 "후보"다 — 한 번 검색해본 것까지 "이 사람의
# 관심사"라고 넣으면 자비스는 스쳐간 호기심을 붙들고 늘어지는 비서가 된다. 시드는 것도 뺀다.
AWAKE = (Trend.RISING, Trend.STEADY)


@dataclass
class TopicProvider:
    """요즘 관심사 — 주제 나무에서 뜨는 중이거나 꾸준한 주제를, 경로와 최근 메모로.

    회고가 매일 가꾸는 나무를 반응·대화가 읽는 자리다. 옛 믿음은 대화만 읽었고 먼저 말 거는 쪽은
    안 읽었다. "어젯밤 늦게 잠들었다" 옆에 "요즘 자소서를 쓴다"가 있어야 말이 사람 얘기가 된다.

    보고서는 더 넓게 본다 — `trends` 에 시드는 중·처음 보임까지, `detail` 로 근거 수와 근거가
    있던 날.
    """

    topics: TopicSource
    name: str = "topics"
    trends: Tuple[Trend, ...] = AWAKE
    detail: bool = False

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        board = [
            s
            for s in standings(self.topics.load(), self.topics.grounds(), now)
            if s.trend in self.trends
        ]
        if not board:
            return None
        notes = self.topics.latest_notes(1)
        lines: List[str] = []
        for s in board:
            line = (
                f"- {' › '.join(s.path)} — {s.trend.value}"
                f"(최근 {RECENT_DAYS}일 중 {s.recent_days}일)"
            )
            if self.detail and s.last is not None:
                # "근거가 있던 날"이라고 쓴다 — "N일"만 쓰면 모델이 연속 일수로 읽었다
                # (2026-11-05 보고서 "43일째 꾸준").
                line += (
                    f", 근거 {s.count}개 · 근거가 있던 날 {s.days}일 · 마지막 {local(s.last):%m-%d}"
                )
            latest = notes.get(s.node.id)
            if latest:
                line += f": {latest[0].text}"
            lines.append(line)
        return ContextBlock(label="요즘 관심사(주제 나무)", body="\n".join(lines))


@dataclass
class EventDigestProvider:
    """최근 사건(위치 등)을 날마다 한 줄로 — 언제 어디를 벗어났고 어디서 탭했나.

    사건은 출근 트리거만 보고 맥락에는 없었다. 그래서 "어제 몇 시에 나갔지?"에 답할 재료가 없었다.
    사건 이름의 뜻은 카드가 들고 온다(`EventKind.describe`) — 플랫폼은 "wallet"이 뭔지 모른다.

    날은 **자정**에 바뀌고 온전한 날만 보인다(오늘은 지금까지). 시각으로 창을 자르면 첫날이 잘린 채
    온전한 하루처럼 보여 "그저께 몇 시에 나갔지?"에 틀린 답을 준다. 사건이 없는 날은 "기록 없음" —
    줄을 빼면 그날을 건너뛴 걸 모른다(JAR-028 리뷰 재현).
    """

    events: EventSource
    kinds: Sequence[EventKind]
    name: str = "event_digest"
    # 어제와 오늘. 보고서는 2주를 본다.
    days: int = 2

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        today = local(now).date()
        first = today - timedelta(days=self.days - 1)
        since = datetime.combine(first, time(0), tzinfo=KST)
        lines: List[str] = []
        for card in self.kinds:
            if not card.active:
                continue
            found = sorted(
                (e for e in self.events.recent(card.kind, since) if since <= e.at <= now),
                key=lambda e: e.at,
            )
            if not found:
                continue
            by_day: Dict[date, List[str]] = {}
            for e in found:
                when = local(e.at)
                place = f"(장소 #{e.place_id})" if e.place_id is not None else ""
                by_day.setdefault(when.date(), []).append(
                    f"{when:%H:%M} {card.describe(e.label)}{place}"
                )
            for offset in range(self.days):
                day = first + timedelta(days=offset)
                items = by_day.get(day)
                body = " · ".join(items) if items else "기록 없음"
                lines.append(f"- {card.label} {day:%m-%d}({WEEKDAYS[day.weekday()]}): {body}")
        if not lines:
            return None
        label = f"최근 {self.days}일 사건(날은 자정에 바뀐다, 오늘은 지금까지)"
        return ContextBlock(label=label, body="\n".join(lines))
