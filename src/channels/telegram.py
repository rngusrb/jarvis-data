"""텔레그램 봇 채널.

Bot API는 그냥 HTTP POST라서 httpx로 충분하다. python-telegram-bot 같은
프레임워크를 붙이지 않는 이유는, 나중에 iOS 푸시로 갈아탈 때
그 라이브러리의 색깔이 코드에 묻어 있으면 걷어내기 번거로워서다.
"""

from __future__ import annotations

from typing import Optional

import httpx

API_ROOT = "https://api.telegram.org"


class TelegramChannel:
    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> None:
        if not bot_token or not chat_id:
            raise ValueError("bot_token과 chat_id가 모두 필요하다")
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def send(self, text: str) -> None:
        # 토큰이 URL에 들어가므로 이 URL은 절대 로그에 찍지 않는다.
        url = f"{API_ROOT}/bot{self._bot_token}/sendMessage"
        response = await self._client.post(
            url,
            json={
                "chat_id": self._chat_id,
                "text": text,
                "disable_notification": False,
            },
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
