"""자비스 실행 진입점.

한 프로세스 안에서 두 가지가 같이 돈다:

  - 수신구(FastAPI)  : 아이폰 단축어가 관측치를 밀어넣는다
  - 자비스 루프      : 주기적으로 데이터를 보고 먼저 말을 건다

띄우기:  uvicorn app.main:app --host 0.0.0.0 --port 8100
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import time, timedelta
from typing import AsyncIterator, List, Optional

import httpx
from fastapi import FastAPI

from src.brain.agent import JarvisAgent
from src.brain.badsites import BadSites
from src.brain.browse import BrowseAgent
from src.brain.browser import Browser
from src.brain.client import VLLMClient
from src.brain.converse import Conversationalist
from src.brain.feedback import FeedbackProvider
from src.brain.gate import Gate
from src.brain.providers import (
    Collected,
    CollectionStatusProvider,
    EventDigestProvider,
    ObservationTrendProvider,
    RecentValuesProvider,
    SpeechHistoryProvider,
    TopicProvider,
    TraceDigestProvider,
)
from src.brain.reflect import Reflector
from src.brain.report import Reporter
from src.brain.research import TopicResearch, interests
from src.brain.tools import ToolLoop
from src.brain.web import PageReader, SearxSearch, reader_client
from src.channels.base import Channel
from src.channels.console import ConsoleChannel
from src.channels.telegram import TelegramChannel
from src.core.config import Settings, load_settings
from src.core.events import EventRegistry
from src.core.logs import hide_in_logs
from src.core.metrics import MetricRegistry
from src.core.topics import Trend
from src.core.traces import TraceRegistry
from src.runtime.daily import DailyLoop
from src.runtime.ingest import router as ingest_router
from src.runtime.listen import ListenLoop
from src.runtime.loop import JarvisLoop
from src.sectors import commute, health, interest
from src.sectors.commute.triggers import FAMILY as COMMUTE_FAMILY
from src.sectors.health.triggers import FAMILY as HEALTH_FAMILY
from src.storage.catalog import MergedCatalog
from src.storage.conversation import SQLiteConversation
from src.storage.events import SQLiteEventStore
from src.storage.places import SQLitePlaceStore
from src.storage.speech import SQLiteSpeechLog
from src.storage.sqlite import SQLiteStore
from src.storage.topics import SQLiteTopicStore
from src.storage.traces import SQLiteTraceStore
from src.triggers.collection import CollectionWatch, Watched
from src.triggers.topics import FAMILY as TOPIC_FAMILY
from src.triggers.topics import TopicWatch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# 켜져 있는 섹터. 각 섹터는 METRICS 와 TRIGGERS 를 내보낸다.
SECTORS = [health, commute, interest]

# 매일 회고가 도는 시각(한국 시간). 하루는 새벽 5시에 바뀐다(interest 의 하루 요약) — 그 뒤라
# 어제가 다 들어와 있고, 아침에 일어나기 전이라 그날 자비스의 맥락에 새 나무가 들어간다.
# 되감기도 이 시각을 쓴다.
REFLECT_AT = time(5, 30)


def build_channel(settings: Settings) -> Channel:
    """텔레그램 설정이 없으면 콘솔로 떨어진다 — 알림 없이도 돌려볼 수 있게."""
    if settings.telegram_enabled:
        return TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id)
    logger.warning("텔레그램 설정이 없다 — 콘솔로 출력한다")
    return ConsoleChannel()


def build_reflector(settings: Settings) -> Reflector:
    """회고를 조립한다.

    수신구와 따로 떼어둔 건 CLI(`app/reflect.py`)가 같은 걸 쓰기 때문이다.
    섹터를 아는 파일은 여전히 이 파일 하나여야 해서(불변식이 집행한다),
    CLI 는 섹터가 아니라 이 함수를 가져다 쓴다.
    """
    trace_kinds = TraceRegistry()
    for sector in SECTORS:
        trace_kinds.register(sector.TRACES)
    return Reflector(
        # 회고는 입력이 수만 토큰이라 판단보다 오래 걸린다. 되감기 실측 하루 20~120초.
        # 응답 상한 16,384: 흔적이 많은 날은 연산 목록이 8,192 를 넘어 잘렸다(되감기 87일 중 3일).
        # 프롬프트 몫과의 합은 Reflector.budget_chars 설명에.
        reasoner=VLLMClient(
            settings.brain_base_url,
            model=settings.brain_model or None,
            max_tokens=16384,
            timeout=600.0,
        ),
        traces=SQLiteTraceStore(settings.db_path),
        topics=SQLiteTopicStore(settings.db_path),
        kinds=trace_kinds.all(),
    )


def build_tools(
    settings: Settings,
    searches: int = 4,
    rounds: int = 4,
    reads: int = 3,
    browse: Optional[BrowseAgent] = None,
) -> Optional[ToolLoop]:
    """웹을 찾아보는 도구 고리. 검색 주소가 없으면 None — 자비스는 아는 것으로만 답한다.

    대화·보고서·관심사 준비가 쓴다. vLLM 이 도구 호출을 켜고 떠 있어야 한다(SETUP — `qwen3_coder`).
    브라우저(`browse`)는 대화만 붙인다 — 오래 걸린다(`build_browse`).
    """
    if not settings.search_base_url:
        return None
    web = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
    return ToolLoop(
        # 찾은 것을 읽고 답하는 차례는 생각을 켠다 — 생각 몫까지 답 상한을 넉넉히. 2026-11-05 실측:
        # 보통 10~40초, 가장 길게 122초(검색 네 번).
        client=VLLMClient(
            settings.brain_base_url,
            model=settings.brain_model or None,
            max_tokens=8192,
            timeout=300.0,
            temperature=0.4,
        ),
        search=SearxSearch(settings.search_base_url, web),
        # 읽기는 따로 — 접속하는 순간 검사한 주소로만 붙는 클라이언트(DNS 리바인딩 막기).
        reader=PageReader(reader_client()),
        searches=searches,
        rounds=rounds,
        reads=reads,
        browse=browse,
    )


def build_browse(settings: Settings) -> Optional[BrowseAgent]:
    """브라우저를 다루는 손(JAR-036~038). 꺼져 있거나 playwright 가 없으면 None — 이유를 남긴다.

    손은 사용자의 기록을 안 본다(목표만 받는다). 크롬은 보안 격리를 켜고 뜬다 — 우분투 23.10+ 는
    AppArmor 프로필이 있어야 한다(SETUP). 못 뜨면 쓸 때마다 시끄럽게 실패한다.
    """
    if not settings.browser:
        logger.info("브라우저 끔 — JARVIS_BROWSER=0")
        return None
    try:
        import playwright  # noqa: F401
    except ImportError:
        logger.warning("브라우저 끔 — playwright 가 없다(docs/SETUP.md 의 브라우저)")
        return None
    # 문제를 일으킨 사이트는 30일 동안 안 간다. 지난번 브라우저로 보다 자비스가 쓰러졌으면
    # 그 사이트도 이제 적는다.
    bad = BadSites(settings.db_path.parent / "browser_bad_sites.json")
    bad.recover()
    return BrowseAgent(
        # 누를 곳을 고르는 차례는 짧다 — 생각 없이, 답 상한도 작게.
        client=VLLMClient(
            settings.brain_base_url,
            model=settings.brain_model or None,
            max_tokens=2048,
            timeout=90.0,
            temperature=0.2,
        ),
        browser=Browser(browsers_path=settings.browsers_path, bad=bad),
    )


def _research(
    settings: Settings, limit: timedelta = timedelta(minutes=2)
) -> Optional[TopicResearch]:
    """찾아 두기. 한도를 낮게 — 검색 3번·읽기 2번·차례 3번, 시간 상한(관심사 준비 2분)."""
    tools = build_tools(settings, searches=3, rounds=3, reads=2)
    return TopicResearch(tools, limit=limit) if tools is not None else None


def build_reporter(settings: Settings) -> Reporter:
    """`/report` 를 조립한다 — 넓게 본다. 서버와 손으로 돌리는 시험이 같은 조립을 쓴다."""
    metrics = MetricRegistry()
    trace_kinds = TraceRegistry()
    event_kinds = EventRegistry()
    for sector in SECTORS:
        metrics.register(sector.METRICS)
        trace_kinds.register(sector.TRACES)
        event_kinds.register(sector.EVENTS)
    store = SQLiteStore(settings.db_path)
    traces = SQLiteTraceStore(settings.db_path)
    events = SQLiteEventStore(settings.db_path)
    cards: List[Collected] = [*metrics.all(), *trace_kinds.all(), *event_kinds.all()]
    return Reporter(
        # 보고서는 두세 쪽이라 답 상한을 넉넉히. 재료가 1~2만 자라 1분 남짓 걸린다.
        reasoner=VLLMClient(
            settings.brain_base_url,
            model=settings.brain_model or None,
            max_tokens=6000,
            timeout=600.0,
            temperature=0.5,
        ),
        providers=(
            # 잠잠한 것만 빼고 전부, 근거 수와 근거가 있던 날까지.
            TopicProvider(
                topics=SQLiteTopicStore(settings.db_path),
                trends=(Trend.RISING, Trend.STEADY, Trend.FADING, Trend.NEW),
                detail=True,
            ),
            TraceDigestProvider(
                source=traces,
                kinds=trace_kinds.all(),
                window=timedelta(days=14),
                include_sensitive=True,
            ),
            # 90일 값을 다 보인다 — 14개만 보이면 석 달 중 14일만 기록된 것으로 읽힌다(리뷰 재현).
            RecentValuesProvider(
                source=store, metrics=metrics.all(), window=timedelta(days=90), max_points=90
            ),
            EventDigestProvider(events=events, kinds=event_kinds.all(), days=14),
            # 접은 지표는 뺀다 — 보고서가 그걸 "모르는 것"으로 적었다(providers 참고).
            CollectionStatusProvider(
                catalog=MergedCatalog(store, traces, events), cards=cards, retired=False
            ),
        ),
        # "다음 한 주"의 다가오는 일정을 지어내지 않게, 쓰기 전에 요즘 관심사 이름만 들고 찾아 둔다
        # (3분 상한). 쓰기는 생각 없이 — 재료를 들고 생각하며 찾게 했더니 7분 걸렸다(실측).
        research=_research(settings, limit=timedelta(minutes=3)),
        interests=lambda now: interests(SQLiteTopicStore(settings.db_path), now),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    hide_in_logs(settings.telegram_bot_token, settings.ingest_token)
    store = SQLiteStore(settings.db_path)

    # 섹터를 켜는 유일한 자리. 새 섹터를 추가하려면 위 import 와 이 목록에
    # 한 줄씩이면 되고, 플랫폼은 열지 않는다.
    metrics = MetricRegistry()
    trace_kinds = TraceRegistry()
    event_kinds = EventRegistry()
    sector_triggers = []
    event_triggers = []
    for sector in SECTORS:
        metrics.register(sector.METRICS)
        trace_kinds.register(sector.TRACES)
        event_kinds.register(sector.EVENTS)
        sector_triggers.extend(sector.TRIGGERS)
        event_triggers.extend(sector.EVENT_TRIGGERS)
    app.state.metrics = metrics
    app.state.trace_kinds = trace_kinds
    app.state.event_kinds = event_kinds

    # 지표·흔적·사건은 마지막 수신 시각을 한 장으로 합쳐서 본다. 이름이 겹치면
    # 어느 쪽 시각인지 알 수 없으므로 여기서 멈춘다.
    names = [
        *(m.kind for m in metrics.all()),
        *(t.kind for t in trace_kinds.all()),
        *(e.kind for e in event_kinds.all()),
    ]
    clash = {kind for kind in names if names.count(kind) > 1}
    if clash:
        raise ValueError(f"지표·흔적·사건의 이름이 겹친다: {sorted(clash)}")

    # 흔적·사건은 관측치와 같은 DB 파일에 산다 — 백업 경로를 하나로 두려는 것.
    traces = SQLiteTraceStore(settings.db_path)
    events = SQLiteEventStore(settings.db_path)
    places = SQLitePlaceStore(settings.db_path)
    catalog = MergedCatalog(store, traces, events)
    # 지표·흔적·사건 카드를 한 줄로. 수집 경로 현황과 /status 가 같이 본다.
    cards: List[Collected] = [*metrics.all(), *trace_kinds.all(), *event_kinds.all()]

    collection = CollectionWatch(
        sources=[
            Watched(m.kind, m.label, m.collector or "", m.via or "기기 미상", m.stale_after)
            for m in metrics.active()
        ]
        + [
            Watched(t.kind, t.label, t.collector or "", t.via or "기기 미상", t.stale_after)
            for t in trace_kinds.active()
        ]
        + [
            Watched(e.kind, e.label, e.collector or "", e.via or "기기 미상", e.stale_after)
            for e in event_kinds.active()
        ],
        # 기기 전체가 끊겼을 때 무엇부터 볼지. 배포 환경의 사실이라 여기서 정한다.
        hints={
            "아이폰": "아이폰에서 Tailscale이 켜져 있는지",
            "맥": "맥이 켜져 있는지, 맥 수집기(launchd)가 도는지",
        },
    )
    # 발화 기억은 DB에 남긴다. 메모리에만 두면 재시작할 때마다 쿨다운이
    # 풀려서, 주 단위로 말하기로 한 신호가 배포할 때마다 되풀이된다.
    gate = Gate(
        log=SQLiteSpeechLog(settings.db_path),
        cooldown_overrides={
            # 수집 중단은 상태가 계속 유지된다. 하루 한 번이면 충분하다.
            collection.name: timedelta(days=1),
            # 만성 신호는 상태가 계속 유지된다. 기본 6시간으로 두면
            # "요즘 잠이 부족하네요"를 하루 네 번 듣는다. 신호의 at 이 마지막 밤이라 새 밤이
            # 들어와야 다시 판단된다 — 새 밤이 매일 오면 7일에 한 번, 안 오면 다시 안 묻는다.
            "chronic_short_sleep": timedelta(days=7),
            # 수면 급감·늦게 잠듦은 여기 없다. 같은 밤은 게이트가 신호의 `at` 으로 한 번만
            # 통과시킨다. late_sleep 에 16시간을 줬을 땐 그날 밤 한 번 더 물으면서 다음 날 아침의
            # 새 밤까지 오후로 밀었다(JAR-018 리뷰 재현).
        },
        # 관심사 얘기는 주제마다 트리거가 따로라(`topic:12`) 묶어서 센다 — 말한 뒤 6시간.
        # 말 걸 시간대가 10~21시(11시간)라 하루 두 번까지다(12시간이면 한 번뿐 — JAR-024 리뷰).
        # 초기엔 수다스럽게, 줄이는 건 👍👎 로(2026-10-07 결정). 신호 자체가 주 두 개쯤이라
        # (백테스트) 간격은 상한일 뿐이다.
        # 건강·이동도 줄기로 센다(2026-10-07 결정, JAR-029). 같은 주기에 울린 것은 루프가 한
        # 판단으로 묶고, 말한 뒤 6시간은 같은 줄기 얘기를 쉰다. 이동은 나선 때(07:40)와 탄
        # 때(07:43)가 따로 들어오는데, 둘째는 받은 지 3시간(FRESH_FOR)이 지나면 안 울려서 한
        # 아침에 하나가 된다. 건강은 "한 밤에 하나"가 아니라 "6시간에 하나"다 — 쉬는 동안 막힌
        # 같은 밤 신호(만성 부족 등)는 기록이 없어 6시간 뒤 다시 판단되고, URGENT 도 같이
        # 기다린다(BACKLOG, 리뷰 재현). 6시간은 관심사와 같게 잡은 것이다 — 판단 기록이 쌓이면
        # 다시 본다(지금은 수면이 안 들어온다).
        family_cooldowns={
            TOPIC_FAMILY: timedelta(hours=6),
            HEALTH_FAMILY: timedelta(hours=6),
            COMMUTE_FAMILY: timedelta(hours=6),
        },
    )
    agent = JarvisAgent(
        reasoner=VLLMClient(settings.brain_base_url, model=settings.brain_model or None),
        gate=gate,
        providers=(
            ObservationTrendProvider(),
            # 게이트와 같은 기억을 본다 — 무슨 말을 했는지 알아야 반복을 피한다.
            SpeechHistoryProvider(log=gate.log),
            # 자기 수집 구조를 모르면 "배터리 최적화를 확인하라" 같은,
            # 이 시스템에 존재하지도 않는 조언을 지어낸다.
            CollectionStatusProvider(catalog=catalog, cards=cards),
            # 수집 "현황"만 알고 "값"을 모르면 자비스가 자기 데이터를 두고도
            # 판단을 못 한다. 실제로 그랬다.
            RecentValuesProvider(source=store, metrics=metrics.all()),
            # 흔적은 낱줄로 못 넣는다. 하루 요약이 있는 종류(앱 사용, 웹 방문)를 어제·오늘치로.
            # 요약이 있는 **모든** 민감 종류를 넣는다(지금은 웹 방문 — 검색어) — 실험 단계라 허들을
            # 낮춰 자비스가 보게 한다(2026-11-02 사용자 결정). 거르는 건 4단계(BACKLOG).
            TraceDigestProvider(source=traces, kinds=trace_kinds.all(), include_sensitive=True),
            # 회고가 매일 가꾸는 주제 나무에서 뜨는 중·꾸준한 주제. 대화도 같은 것을 본다.
            TopicProvider(topics=SQLiteTopicStore(settings.db_path)),
            # 내가 먼저 건 말에 사용자가 👍👎 한 것. 줄이는 판단을 모델이 먼저 읽는다.
            FeedbackProvider(log=gate.log),
            # 어제·오늘 위치 사건 — 사건은 출근 트리거만 보고 맥락엔 없어서 "어제 몇 시에
            # 나갔지?"에 답할 재료가 없었다(JAR-028).
            EventDigestProvider(events=events, kinds=event_kinds.all()),
        ),
    )
    # `/report` — 넓게 모아 "요즘의 너"를 쓴다(JAR-027). 손으로 시험할 때도 같은 조립을 쓴다.
    reporter = build_reporter(settings)
    app.state.reporter = reporter

    app.state.settings = settings
    app.state.store = store
    app.state.traces = traces
    app.state.events = events
    app.state.catalog = catalog
    app.state.places = places

    # 웹 검색 도구 고리 — 대화가 쓴다. 관심사 준비와 보고서는 따로, 낮은 한도로(`_research`).
    tools = build_tools(settings, browse=build_browse(settings))
    if tools is None:
        logger.info("웹 검색 끔 — JARVIS_SEARCH_URL 이 비었다")
    # 대화 기록. 듣는 루프가 주고받은 말을, 반응 루프가 먼저 건 말을 적는다 — 둘이 한 흐름이어야
    # 사용자가 먼저 건 말에 답할 때 자비스가 무슨 말에 대한 답인지 안다(2026-11-05 사용자 결정).
    conversation = SQLiteConversation(settings.db_path)
    jarvis = JarvisLoop(
        source=store,
        triggers=sector_triggers,
        agent=agent,
        channel=build_channel(settings),
        watches=[collection],
        snapshot=catalog.last_seen,
        event_triggers=event_triggers,
        events=events,
        # 회고가 가꾸는 나무에서 요즘 뜨는 관심사로 먼저 말을 건다(한 주기에 하나).
        topic_watch=TopicWatch(),
        topics=SQLiteTopicStore(settings.db_path),
        transcript=conversation,
        # 관심사로 말 걸기 전에 지금 도움이 될 것(다가오는 일정 등)을 찾아 둔다 — 주 두 번쯤.
        research=_research(settings),
    )
    # 수집 훅(app/ingest)이 데이터를 받자마자 이걸 한 번 더 돌린다.
    app.state.jarvis = jarvis

    # 두 번째 루프. 첫 번째가 신호에 **반응**한다면 이건 쌓인 흔적을
    # **회고**한다. 재료도 주기도 달라서 같은 루프에 넣을 수 없다 —
    # "어젯밤 2.7시간"은 30분 안에 말해야 하고, "요즘 이사를 알아보네"는
    # 일주일치가 모여야 보인다.
    reflector = build_reflector(settings)
    app.state.reflector = reflector
    daily = DailyLoop(
        job=reflector.run_once,
        at=REFLECT_AT,
        last_run=reflector.topics.last_run,
        name="회고",
    )

    tasks = [
        asyncio.create_task(jarvis.run_forever(settings.loop_interval_sec)),
        asyncio.create_task(daily.run_forever()),
    ]

    # 세 번째 루프. 앞의 둘은 시계가 돌리고 이건 사용자가 돌린다.
    # 텔레그램이 없으면 들을 곳도 없으므로 그때는 안 띄운다.
    channel = jarvis.channel
    if isinstance(channel, TelegramChannel):
        listener = ListenLoop(
            source=channel,
            responder=Conversationalist(
                reasoner=VLLMClient(settings.brain_base_url, model=settings.brain_model or None),
                topics=SQLiteTopicStore(settings.db_path),
                catalog=catalog,
                source=store,
                history=conversation,
                providers=agent.providers,
                stale_after={c.kind: c.stale_after for c in cards},
                # 게이트와 같은 기억 — 👍👎 를 남기고 /status 가 먼저 건 말을 센다.
                speech=gate.log,
                reporter=reporter,
                # 바뀌는 사실(일정·가격·뉴스)은 찾아보고 답한다. 찾기 시작하면 "찾아볼게"를 먼저.
                tools=tools,
            ),
            speaker=channel,
            log=conversation,
        )
        tasks.append(asyncio.create_task(listener.run_forever()))
        logger.info("듣는 루프 기동 — 대화 %d건", conversation.count())
    else:
        logger.info("텔레그램이 아니라 듣는 루프는 띄우지 않는다 (채널=%s)", channel.name)
    logger.info(
        "자비스 기동 — 관측치 %d건, 흔적 %d건, 주기 %d초",
        store.count(),
        app.state.traces.count(),
        settings.loop_interval_sec,
    )

    try:
        yield
    finally:
        for running in tasks:
            running.cancel()


app = FastAPI(title="jarvis", lifespan=lifespan)
app.include_router(ingest_router)
