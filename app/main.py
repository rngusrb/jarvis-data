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
from typing import AsyncIterator, List

from fastapi import FastAPI

from src.brain.agent import JarvisAgent
from src.brain.client import VLLMClient
from src.brain.converse import Conversationalist
from src.brain.gate import Gate
from src.brain.providers import (
    Collected,
    CollectionStatusProvider,
    ObservationTrendProvider,
    RecentValuesProvider,
    SpeechHistoryProvider,
    TopicProvider,
    TraceDigestProvider,
)
from src.brain.reflect import Reflector
from src.channels.base import Channel
from src.channels.console import ConsoleChannel
from src.channels.telegram import TelegramChannel
from src.core.config import Settings, load_settings
from src.core.events import EventRegistry
from src.core.logs import hide_in_logs
from src.core.metrics import MetricRegistry
from src.core.traces import TraceRegistry
from src.runtime.daily import DailyLoop
from src.runtime.ingest import router as ingest_router
from src.runtime.listen import ListenLoop
from src.runtime.loop import JarvisLoop
from src.sectors import commute, health, interest
from src.storage.catalog import MergedCatalog
from src.storage.conversation import SQLiteConversation
from src.storage.events import SQLiteEventStore
from src.storage.places import SQLitePlaceStore
from src.storage.speech import SQLiteSpeechLog
from src.storage.sqlite import SQLiteStore
from src.storage.topics import SQLiteTopicStore
from src.storage.traces import SQLiteTraceStore
from src.triggers.collection import CollectionWatch, Watched

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
        ),
    )

    app.state.settings = settings
    app.state.store = store
    app.state.traces = traces
    app.state.events = events
    app.state.catalog = catalog
    app.state.places = places

    jarvis = JarvisLoop(
        source=store,
        triggers=sector_triggers,
        agent=agent,
        channel=build_channel(settings),
        watches=[collection],
        snapshot=catalog.last_seen,
        event_triggers=event_triggers,
        events=events,
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
        conversation = SQLiteConversation(settings.db_path)
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
