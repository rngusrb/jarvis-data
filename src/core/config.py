"""환경변수 기반 설정.

비밀값(봇 토큰 등)은 코드나 git에 절대 두지 않고 .env로만 받는다.
.env는 이미 .gitignore에 있다.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 데스크탑 서버의 Tailscale 주소. vLLM이 OpenAI 호환 API로 여기 떠 있다.
# :8001(local-llm-agent)이 아니라 :8000을 직접 부른다 — 자비스의 판단 로직은
# 이 레포(src/brain)가 갖고, 저쪽은 순수 추론기로만 쓰기로 했다.
DEFAULT_BRAIN_URL = "http://100.98.90.38:8000"
# 같은 데스크탑의 SearXNG(웹 검색). 비우면 자비스는 검색하지 않는다.
DEFAULT_SEARCH_URL = "http://100.98.90.38:8888"
# 리눅스 서버에서 크롬이 있는 곳 — root 가 가진 폴더. 크롬의 보안 격리를 허락하는 AppArmor 프로필이
# 이 경로에만 붙는다(사용자가 고칠 수 있는 경로면 아무 실행 파일이나 격리 허락을 얻는다 — JAR-036
# 리뷰).
DEFAULT_BROWSERS_PATH = "/opt/jarvis/ms-playwright" if sys.platform.startswith("linux") else ""


@dataclass(frozen=True)
class Settings:
    brain_base_url: str
    brain_model: str
    telegram_bot_token: str
    telegram_chat_id: str
    loop_interval_sec: int
    db_path: Path
    ingest_token: str
    # 웹 검색(SearXNG). 비어 있으면 도구 고리를 안 붙인다 — 시험이 만드는 설정은 비어 있다.
    search_base_url: str = ""
    # 대화가 브라우저(헤드리스 크롬)를 다룰 수 있나. 시험이 만드는 설정은 끔. 켜도 playwright 가
    # 없으면 앱이 끄고 남긴다(SETUP 의 브라우저).
    browser: bool = False
    # 크롬이 있는 폴더(비우면 Playwright 기본 자리 — 개발 맥).
    browsers_path: str = ""

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
        db_path=Path(os.getenv("JARVIS_DB_PATH", "data/jarvis.db")),
        # 아이폰 단축어가 들고 올 토큰. Tailscale 안이라도 한 겹 더 둔다.
        ingest_token=os.getenv("JARVIS_INGEST_TOKEN", ""),
        search_base_url=os.getenv("JARVIS_SEARCH_URL", DEFAULT_SEARCH_URL).rstrip("/"),
        browser=os.getenv("JARVIS_BROWSER", "1").strip().lower() not in ("0", "false", "off", ""),
        browsers_path=os.getenv("JARVIS_BROWSERS_PATH", DEFAULT_BROWSERS_PATH).strip(),
    )
