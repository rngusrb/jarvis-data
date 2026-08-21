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
from app.loop import run_forever
from src.brain.agent import JarvisAgent
from src.brain.client import VLLMClient
from src.brain.gate import Gate
from src.brain.providers import ObservationTrendProvider, SpeechHistoryProvider
from src.channels.base import Channel
from src.channels.console import ConsoleChannel
from src.channels.telegram import TelegramChannel
from src.core.config import Settings, load_settings
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

    # 수집 중단은 상태가 계속 유지되는 신호라 기본 쿨다운(6시간)이면
    # 하루 네 번 같은 말을 한다. 하루에 한 번이면 충분하다.
    stale_sleep = StaleDataTrigger(kind="sleep_hours", label="수면")
    gate = Gate(cooldown_overrides={stale_sleep.name: timedelta(days=1)})
    agent = JarvisAgent(
        reasoner=VLLMClient(settings.brain_base_url, model=settings.brain_model or None),
        gate=gate,
        providers=(
            ObservationTrendProvider(),
            # 게이트와 같은 기억을 본다 — 무슨 말을 했는지 알아야 반복을 피한다.
            SpeechHistoryProvider(log=gate.log),
        ),
    )

    app.state.settings = settings
    app.state.store = store

    task = asyncio.create_task(
        run_forever(
            source=store,
            triggers=(SleepDropTrigger(), stale_sleep),
            agent=agent,
            channel=build_channel(settings),
            interval_sec=settings.loop_interval_sec,
        )
    )
    logger.info("자비스 기동 — 관측치 %d건, 주기 %d초", store.count(), settings.loop_interval_sec)

    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="jarvis", lifespan=lifespan)
app.include_router(ingest_router)
