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
from src.brain.gate import Gate
from src.brain.providers import (
    CollectionStatusProvider,
    ObservationTrendProvider,
    SpeechHistoryProvider,
)
from src.brain.reflect import Reflector
from src.channels.base import Channel
from src.channels.console import ConsoleChannel
from src.channels.telegram import TelegramChannel
from src.core.config import Settings, load_settings
from src.core.metrics import MetricRegistry
from src.core.traces import TraceRegistry
from src.runtime.ingest import router as ingest_router
from src.runtime.loop import JarvisLoop
from src.sectors import commute, health, interest
from src.storage.beliefs import SQLiteBeliefStore
from src.storage.speech import SQLiteSpeechLog
from src.storage.sqlite import SQLiteStore
from src.storage.traces import SQLiteTraceStore
from src.triggers.stale import StaleDataTrigger

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
        reasoner=VLLMClient(settings.brain_base_url, model=settings.brain_model or None),
        traces=SQLiteTraceStore(settings.db_path),
        beliefs=SQLiteBeliefStore(settings.db_path),
        kinds=trace_kinds.all(),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
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

    stale = [
        StaleDataTrigger(kind=m.kind, label=m.label, stale_after=m.stale_after)
        for m in metrics.active()
    ]
    # 발화 기억은 DB에 남긴다. 메모리에만 두면 재시작할 때마다 쿨다운이
    # 풀려서, 주 단위로 말하기로 한 신호가 배포할 때마다 되풀이된다.
    gate = Gate(
        log=SQLiteSpeechLog(settings.db_path),
        cooldown_overrides={
            **{t.name: timedelta(days=1) for t in stale},
            # 만성 신호는 상태가 계속 유지된다. 기본 6시간으로 두면
            # "요즘 잠이 부족하네요"를 하루 네 번 듣는다.
            "chronic_short_sleep": timedelta(days=7),
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
            CollectionStatusProvider(catalog=store, metrics=metrics.all()),
        ),
    )

    app.state.settings = settings
    app.state.store = store
    # 흔적은 관측치와 같은 DB 파일에 산다 — 백업 경로를 하나로 두려는 것.
    app.state.traces = SQLiteTraceStore(settings.db_path)

    jarvis = JarvisLoop(
        source=store,
        triggers=[*sector_triggers, *stale],
        agent=agent,
        channel=build_channel(settings),
    )
    # 수집 훅(app/ingest)이 데이터를 받자마자 이걸 한 번 더 돌린다.
    app.state.jarvis = jarvis

    # 두 번째 루프. 첫 번째가 신호에 **반응**한다면 이건 쌓인 흔적을
    # **회고**한다. 재료도 주기도 달라서 같은 루프에 넣을 수 없다 —
    # "어젯밤 2.7시간"은 30분 안에 말해야 하고, "요즘 이사를 알아보네"는
    # 일주일치가 모여야 보인다.
    app.state.reflector = build_reflector(settings)

    task = asyncio.create_task(jarvis.run_forever(settings.loop_interval_sec))
    logger.info(
        "자비스 기동 — 관측치 %d건, 흔적 %d건, 주기 %d초",
        store.count(),
        app.state.traces.count(),
        settings.loop_interval_sec,
    )

    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="jarvis", lifespan=lifespan)
app.include_router(ingest_router)
