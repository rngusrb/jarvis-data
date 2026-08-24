"""환경변수 기반 설정.

비밀값(봇 토큰 등)은 코드나 git에 절대 두지 않고 .env로만 받는다.
.env는 이미 .gitignore에 있다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# 데스크탑 서버의 Tailscale 주소. vLLM이 OpenAI 호환 API로 여기 떠 있다.
# :8001(local-llm-agent)이 아니라 :8000을 직접 부른다 — 자비스의 판단 로직은
# 이 레포(src/brain)가 갖고, 저쪽은 순수 추론기로만 쓰기로 했다.
DEFAULT_BRAIN_URL = "http://100.98.90.38:8000"


@dataclass(frozen=True)
class Settings:
    brain_base_url: str
    brain_model: str
    telegram_bot_token: str
    telegram_chat_id: str
    loop_interval_sec: int

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_settings() -> Settings:
    """.env를 읽어 Settings를 만든다. 값이 없으면 안전한 기본값으로 떨어진다."""
    load_dotenv()
    return Settings(
        brain_base_url=os.getenv("JARVIS_BRAIN_URL", DEFAULT_BRAIN_URL).rstrip("/"),
        # 비워두면 /v1/models로 서버에 물어본다.
        brain_model=os.getenv("JARVIS_BRAIN_MODEL", ""),
        telegram_bot_token=os.getenv("JARVIS_TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("JARVIS_TELEGRAM_CHAT_ID", ""),
        loop_interval_sec=int(os.getenv("JARVIS_LOOP_INTERVAL_SEC", "1800")),
    )
