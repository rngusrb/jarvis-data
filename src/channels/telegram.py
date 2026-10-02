"""텔레그램 봇 채널 — 보내기와 받기.

Bot API는 그냥 HTTP라서 httpx로 충분하다. python-telegram-bot 같은
프레임워크를 붙이지 않는 이유는, 나중에 iOS 푸시로 갈아탈 때
그 라이브러리의 색깔이 코드에 묻어 있으면 걷어내기 번거로워서다.

## 왜 웹훅이 아니라 폴링인가

텔레그램이 메시지를 주는 방식이 둘인데 하나는 못 쓴다.

    웹훅   텔레그램이 우리 서버로 POST   공개 HTTPS 주소가 필요
    폴링   우리가 나가서 물어본다        뚫을 게 없다

서버가 Tailscale 안에만 있다. 개인 건강 데이터 서버를 인터넷에 노출하는 건
이 프로젝트의 전제를 깨는 일이라, 나가서 묻는 쪽을 택했다.

long polling(`timeout`)을 쓰므로 30초 대기 한 번에 요청 하나다. 짧은 주기로
계속 두드리는 것보다 싸고, 메시지가 오면 즉시 돌아와서 반응도 빠르다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from src.channels.base import Incoming

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"


class TelegramChannel:
    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
        # long polling 대기. 이보다 길게 잡으면 종료가 그만큼 늦어진다.
        poll_seconds: int = 30,
    ) -> None:
        if not bot_token or not chat_id:
            raise ValueError("bot_token과 chat_id가 모두 필요하다")
        self._bot_token = bot_token
        self._chat_id = str(chat_id)
        # 폴링은 대기 시간만큼 더 기다려야 한다. 안 그러면 매번 타임아웃이다.
        self._client = client or httpx.AsyncClient(timeout=timeout + poll_seconds)
        self._poll_seconds = poll_seconds

    def _url(self, method: str) -> str:
        # 토큰이 경로에 들어간다. 이 문자열은 절대 로그에 찍지 않는다.
        return f"{API_ROOT}/bot{self._bot_token}/{method}"

    async def send(self, text: str, buttons: Optional[List[Dict[str, str]]] = None) -> None:
        payload: Dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_notification": False,
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": b["text"], "callback_data": b["action"]} for b in buttons]
                ]
            }
        response = await self._client.post(self._url("sendMessage"), json=payload)
        response.raise_for_status()

    async def receive(self, since: int) -> List[Incoming]:
        """`since` 이후 도착한 것을 가져온다.

        offset 을 `since + 1` 로 주면 그보다 앞선 것은 텔레그램 쪽에서
        확정 처리된다. 같은 메시지를 두 번 처리하지 않으려면 커서를 반드시
        저장해야 한다 — 저장을 빼먹으면 재시작할 때마다 하루치를 다시 읽는다.
        """
        response = await self._client.post(
            self._url("getUpdates"),
            json={
                "offset": since + 1,
                "timeout": self._poll_seconds,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            logger.warning("텔레그램 getUpdates 실패: %s", body.get("description"))
            return []

        found: List[Incoming] = []
        for update in body.get("result") or []:
            parsed = self._parse(update)
            if parsed is not None:
                found.append(parsed)
        return found

    def _parse(self, update: Dict[str, Any]) -> Optional[Incoming]:
        cursor = int(update.get("update_id", 0))

        callback = update.get("callback_query")
        if callback:
            message = callback.get("message") or {}
            if not self._is_owner(message.get("chat") or {}):
                return None
            return Incoming(
                text=str(message.get("text", "")),
                at=datetime.now(timezone.utc),
                cursor=cursor,
                action=str(callback.get("data", "")),
                meta={"callback_id": callback.get("id")},
            )

        message = update.get("message") or update.get("edited_message")
        if not message:
            return None
        chat = message.get("chat") or {}
        if not self._is_owner(chat):
            return None
        text = str(message.get("text", "")).strip()
        if not text:
            # 사진·스티커 등. 지금은 다룰 줄 모르지만 커서는 넘겨야 한다.
            return Incoming(text="", at=self._sent_at(message), cursor=cursor)
        return Incoming(text=text, at=self._sent_at(message), cursor=cursor)

    def _is_owner(self, chat: Dict[str, Any]) -> bool:
        """주인이 보낸 것인가.

        봇 사용자명은 공개라 아무나 말을 걸 수 있다. 거르지 않으면 남이
        주인의 수면·위치를 물어볼 수 있다. 위쪽 레이어에 맡기면 언젠가
        빠뜨리므로 여기서 막는다.
        """
        if str(chat.get("id")) == self._chat_id:
            return True
        logger.warning("주인이 아닌 chat 에서 온 메시지를 버렸다 (id=%s)", chat.get("id"))
        return False

    @staticmethod
    def _sent_at(message: Dict[str, Any]) -> datetime:
        epoch = message.get("date")
        if isinstance(epoch, int):
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        return datetime.now(timezone.utc)

    async def ack(self, callback_id: str) -> None:
        """버튼 누름에 응답한다. 안 하면 폰에서 로딩 표시가 계속 돈다."""
        await self._client.post(
            self._url("answerCallbackQuery"), json={"callback_query_id": callback_id}
        )

    async def aclose(self) -> None:
        await self._client.aclose()
