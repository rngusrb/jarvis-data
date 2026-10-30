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
from datetime import timedelta
from typing import AsyncIterator

from fastapi import FastAPI

from src.brain.agent import JarvisAgent
from src.brain.client import VLLMClient
from src.brain.converse import Conversationalist
from src.brain.gate import Gate
from src.brain.providers import (
    CollectionStatusProvider,
    ObservationTrendProvider,
    RecentValuesProvider,
    SpeechHistoryProvider,
)
from src.brain.reflect import Reflector
from src.channels.base import Channel
from src.channels.console import ConsoleChannel
from src.channels.telegram import TelegramChannel
from src.core.config import Settings, load_settings
from src.core.logs import hide_in_logs
from src.core.metrics import MetricRegistry
from src.core.traces import TraceRegistry
from src.runtime.ingest import router as ingest_router
from src.runtime.listen import ListenLoop
from src.runtime.loop import JarvisLoop
from src.sectors import commute, health, interest
from src.storage.beliefs import SQLiteBeliefStore
from src.storage.conversation import SQLiteConversation
from src.storage.speech import SQLiteSpeechLog
from src.storage.sqlite import SQLiteStore
from src.storage.traces import SQLiteTraceStore
from src.triggers.collection import CollectionWatch, Watched

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# 켜져 있는 섹터. 각 섹터는 METRICS 와 TRIGGERS 를 내보낸다.
SECTORS = [health, commute, interest]


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
        # 회고는 입력이 3만 토큰대라 판단보다 오래 생각한다. 실측 44~56초, 한도는 넉넉히.
        reasoner=VLLMClient(
            settings.brain_base_url,
            model=settings.brain_model or None,
            max_tokens=8192,
            timeout=300.0,
        ),
        traces=SQLiteTraceStore(settings.db_path),
        beliefs=SQLiteBeliefStore(settings.db_path),
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
    sector_triggers = []
    for sector in SECTORS:
        metrics.register(sector.METRICS)
        trace_kinds.register(sector.TRACES)
        sector_triggers.extend(sector.TRIGGERS)
    app.state.metrics = metrics
    app.state.trace_kinds = trace_kinds

    # 지표와 흔적은 마지막 수신 시각을 한 장으로 합쳐서 본다. 이름이 겹치면
    # 어느 쪽 시각인지 알 수 없으므로 여기서 멈춘다.
    clash = {m.kind for m in metrics.all()} & {t.kind for t in trace_kinds.all()}
    if clash:
        raise ValueError(f"지표와 흔적의 이름이 겹친다: {sorted(clash)}")

    collection = CollectionWatch(
        sources=[
            Watched(m.kind, m.label, m.collector or "", m.via or "기기 미상", m.stale_after)
            for m in metrics.active()
        ]
        + [
            Watched(t.kind, t.label, t.collector or "", t.via or "기기 미상", t.stale_after)
            for t in trace_kinds.active()
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
            # "요즘 잠이 부족하네요"를 하루 네 번 듣는다.
            "chronic_short_sleep": timedelta(days=7),
        },
    )
    agent = JarvisAgent(
        # 회고는 입력이 3만 토큰대라 판단보다 오래 생각한다. 실측 44~56초, 한도는 넉넉히.
        reasoner=VLLMClient(
            settings.brain_base_url,
            model=settings.brain_model or None,
            max_tokens=8192,
            timeout=300.0,
        ),
        gate=gate,
        providers=(
            ObservationTrendProvider(),
            # 게이트와 같은 기억을 본다 — 무슨 말을 했는지 알아야 반복을 피한다.
            SpeechHistoryProvider(log=gate.log),
            # 자기 수집 구조를 모르면 "배터리 최적화를 확인하라" 같은,
            # 이 시스템에 존재하지도 않는 조언을 지어낸다.
            CollectionStatusProvider(catalog=store, metrics=metrics.all()),
            # 수집 "현황"만 알고 "값"을 모르면 자비스가 자기 데이터를 두고도
            # 판단을 못 한다. 실제로 그랬다.
            RecentValuesProvider(source=store, metrics=metrics.all()),
        ),
    )

    app.state.settings = settings
    app.state.store = store
    # 흔적은 관측치와 같은 DB 파일에 산다 — 백업 경로를 하나로 두려는 것.
    traces = SQLiteTraceStore(settings.db_path)
    app.state.traces = traces

    jarvis = JarvisLoop(
        source=store,
        triggers=sector_triggers,
        agent=agent,
        channel=build_channel(settings),
        watches=[collection],
        snapshot=lambda: {**store.last_seen(), **traces.last_seen()},
    )
    # 수집 훅(app/ingest)이 데이터를 받자마자 이걸 한 번 더 돌린다.
    app.state.jarvis = jarvis

    # 두 번째 루프. 첫 번째가 신호에 **반응**한다면 이건 쌓인 흔적을
    # **회고**한다. 재료도 주기도 달라서 같은 루프에 넣을 수 없다 —
    # "어젯밤 2.7시간"은 30분 안에 말해야 하고, "요즘 이사를 알아보네"는
    # 일주일치가 모여야 보인다.
    app.state.reflector = build_reflector(settings)

    tasks = [asyncio.create_task(jarvis.run_forever(settings.loop_interval_sec))]

    # 세 번째 루프. 앞의 둘은 시계가 돌리고 이건 사용자가 돌린다.
    # 텔레그램이 없으면 들을 곳도 없으므로 그때는 안 띄운다.
    channel = jarvis.channel
    if isinstance(channel, TelegramChannel):
        conversation = SQLiteConversation(settings.db_path)
        listener = ListenLoop(
            source=channel,
            responder=Conversationalist(
                # 회고는 입력이 3만 토큰대라 판단보다 오래 생각한다. 실측 44~56초, 한도는 넉넉히.
                reasoner=VLLMClient(
                    settings.brain_base_url,
                    model=settings.brain_model or None,
                    max_tokens=8192,
                    timeout=300.0,
                ),
                beliefs=SQLiteBeliefStore(settings.db_path),
                catalog=store,
                source=store,
                history=conversation,
                providers=agent.providers,
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
