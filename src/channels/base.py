"""발신 채널 계약.

여기가 이 프로젝트에서 제일 중요한 이음매다. 지금은 텔레그램으로 내보내지만
곧 iOS 푸시/단축어로 갈아탈 예정이고, 그때 위쪽 레이어(app/loop.py)는
단 한 줄도 바뀌면 안 된다. 그래서 채널이 할 줄 아는 일을 send() 하나로 묶어둔다.
"""

from __future__ import annotations

from typing import Protocol


class Channel(Protocol):
    name: str

    async def send(self, text: str) -> None:
        """사용자에게 메시지를 밀어넣는다. 실패하면 예외를 올린다."""
        ...
