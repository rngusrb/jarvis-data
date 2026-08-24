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

from app.ingest import router as ingest_router
from app.loop import JarvisLoop
from src.brain.agent import JarvisAgent
from src.brain.client import VLLMClient
from src.brain.gate import Gate
from src.brain.providers import (
    CollectionStatusProvider,
    ObservationTrendProvider,
    SpeechHistoryProvider,
)
from src.channels.base import Channel
from src.channels.console import ConsoleChannel
from src.channels.telegram import TelegramChannel
from src.core.config import Settings, load_settings
from src.core.metrics import MetricRegistry
from src.sectors.health import METRICS as HEALTH_METRICS
from src.storage.sqlite import SQLiteStore
from src.triggers.sleep import SleepDropTrigger
from src.triggers.stale import StaleDataTrigger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)


def build_channel(settings: Settings) -> Channel:
    """텔레그램 설정이 없으면 콘솔로 떨어진다 — 알림 없이도 돌려볼 수 있게."""
    if settings.telegram_enabled:
        return TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id)
    logger.warning("텔레그램 설정이 없다 — 콘솔로 출력한다")
    return ConsoleChannel()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)

    # 섹터가 자기 지표를 들고 들어온다. 여기가 "어떤 섹터를 켤지" 고르는 유일한 곳이고,
    # 나머지(수신구·감시·맥락)는 전부 이 등록에서 파생된다.
    metrics = MetricRegistry().register(HEALTH_METRICS)
    app.state.metrics = metrics

    # 수집이 멈춘 걸 종류별로 본다. 단축어를 수면용으로만 만들어두면 걸음수는
    # 조용히 죽어 있는데 아무도 알려주지 않는다 — 실제로 그 상태로 며칠을 보냈다.
    stale = [
        StaleDataTrigger(kind=m.kind, label=m.label, stale_after=m.stale_after)
        for m in metrics.active()
    ]
    gate = Gate(cooldown_overrides={t.name: timedelta(days=1) for t in stale})
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

    jarvis = JarvisLoop(
        source=store,
        triggers=[SleepDropTrigger(), *stale],
        agent=agent,
        channel=build_channel(settings),
    )
    # 수집 훅(app/ingest)이 데이터를 받자마자 이걸 한 번 더 돌린다.
    app.state.jarvis = jarvis

    task = asyncio.create_task(jarvis.run_forever(settings.loop_interval_sec))
    logger.info("자비스 기동 — 관측치 %d건, 주기 %d초", store.count(), settings.loop_interval_sec)

    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="jarvis", lifespan=lifespan)
app.include_router(ingest_router)
