"""개발용 채널 — 터미널에 찍기만 한다.

채널을 안 붙였을 때의 기본값이자, 트리거/판단 로직을 다듬는 동안
실제로 알림을 쏘지 않고 결과만 보기 위한 용도.
"""

from __future__ import annotations


class ConsoleChannel:
    name = "console"

    async def send(self, text: str) -> None:
        print(f"[jarvis] {text}")
