"""듣는 루프 — 자비스의 세 번째 루프.

    반응 루프 (30분)    신호를 보고 먼저 말을 건다
    회고 루프 (주 단위)  흔적을 훑어 믿음을 갱신한다
    듣는 루프 (상시)     사용자가 꺼낸 말에 답한다        ← 이 파일

셋을 나눈 이유는 계기가 다르기 때문이다. 앞의 둘은 시계가 돌리고 이건
사용자가 돌린다. 한 루프에 합치면 "30분마다 답장"이 되는데, 그건 대화가
아니다.

이 파일에는 판단이 없다. 받아서 저장하고 넘기는 일만 한다 — 무슨 말을
돌려줄지는 `Responder` 가 정한다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

from src.channels.base import Incoming, Listener
from src.storage.conversation import SQLiteConversation

logger = logging.getLogger(__name__)


class Responder(Protocol):
    """받은 말에 대한 답. 답할 게 없으면 None."""

    async def reply(self, incoming: Incoming, now: datetime) -> Optional[str]: ...


class Speaker(Protocol):
    async def send(self, text: str) -> None: ...


@dataclass
class ListenLoop:
    source: Listener
    responder: Responder
    speaker: Speaker
    log: SQLiteConversation
    cursor_name: str = "telegram"

    async def run_once(self) -> int:
        """한 번 듣는다. 처리한 메시지 수를 돌려준다."""
        since = self.log.cursor(self.cursor_name)
        try:
            batch = await self.source.receive(since)
        except Exception:
            # 듣기 실패가 자비스를 죽이면 안 된다. 네트워크는 흔들린다.
            logger.exception("듣기 실패")
            return 0

        handled = 0
        for incoming in batch:
            # 커서를 **먼저** 민다. 처리하다 터져도 같은 메시지를 영원히
            # 다시 읽는 고리에 빠지지 않게 하려는 것이다. 한 건을 잃는 쪽이
            # 무한 반복보다 낫다.
            self.log.advance(self.cursor_name, incoming.cursor)
            if not incoming.text and not incoming.is_action:
                continue

            self.log.add(
                "user",
                incoming.at,
                incoming.action or incoming.text,
                meta={"action": incoming.action} if incoming.is_action else None,
            )
            try:
                answer = await self.responder.reply(incoming, datetime.now(timezone.utc))
            except Exception:
                logger.exception("답 만들기 실패: %.60s", incoming.text)
                continue

            if answer is None:
                continue
            try:
                await self.speaker.send(answer)
            except Exception:
                logger.exception("답장 발송 실패")
                continue

            self.log.add("jarvis", datetime.now(timezone.utc), answer)
            handled += 1

        return handled

    async def run_forever(self) -> None:
        """계속 듣는다.

        `receive` 가 long polling 이라 자체 대기한다. 여기서 sleep 을 또
        걸면 그만큼 답장이 늦어지므로, 실패했을 때만 쉰다.
        """
        backoff = 1
        while True:
            try:
                await self.run_once()
                backoff = 1
            except Exception:
                logger.exception("듣는 루프 예외")
                await asyncio.sleep(backoff)
                # 텔레그램이 죽었을 때 1초마다 두드리면 서로 손해다.
                backoff = min(backoff * 2, 60)
