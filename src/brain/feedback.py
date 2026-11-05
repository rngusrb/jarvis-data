"""👍👎 — 자비스가 먼저 건 말에 대한 사용자의 반응.

"초기엔 수다스럽게, 줄이는 건 피드백으로"(2026-10-07 사용자 결정)의 피드백 쪽이다. 말을 줄이는
판단을 사람이 미리 정하지 않고, 실제로 들은 반응으로 한다.

버튼의 모양(무엇이 돌아오나)은 여기 한 곳에만 둔다. 보내는 쪽(루프)과 받는 쪽(대화)이 따로 알면
한쪽만 바뀌는 날 버튼이 조용히 아무 일도 안 한다.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from src.brain.context import ContextBlock
from src.brain.memory import SpeechLog
from src.core.clock import local
from src.core.models import Insight

PREFIX = "fb"
MARKS = {1: "👍", -1: "👎"}


def new_token() -> str:
    """말 하나에 붙이는 짧은 표. 텔레그램 버튼 값은 64바이트까지라 말 자체를 실을 수 없다."""
    return secrets.token_hex(4)


def buttons(token: str) -> List[Dict[str, str]]:
    return [
        {"text": MARKS[1], "action": f"{PREFIX}:+:{token}"},
        {"text": MARKS[-1], "action": f"{PREFIX}:-:{token}"},
    ]


def parse(action: str) -> Optional[Tuple[int, str]]:
    """버튼이 들고 온 값을 (반응, 표)로. 이 버튼이 아니면 None."""
    prefix, _, rest = action.partition(":")
    sign, _, token = rest.partition(":")
    if prefix != PREFIX or sign not in ("+", "-") or not token:
        return None
    return (1 if sign == "+" else -1), token


@dataclass
class FeedbackProvider:
    """내가 먼저 건 말에 사용자가 어떻게 반응했나(최근 30일).

    게이트가 숫자로 줄이기 전에, 모델이 먼저 읽고 말투와 거리를 조정한다. 👎 받은 말과 비슷한 말을
    또 하면 사용자는 알림을 끈다 — 알림 스팸이 이 프로덕트의 주된 실패 방식이다.
    """

    log: SpeechLog
    name: str = "feedback"
    window: timedelta = timedelta(days=30)

    def fetch(self, insight: Optional[Insight], now: datetime) -> Optional[ContextBlock]:
        rated = self.log.rated(now - self.window)
        if not rated:
            return None
        lines = [f"- {local(r.at):%m/%d} {MARKS[r.feedback]} {r.text}" for r in rated[-10:]]
        return ContextBlock(label="내가 먼저 건 말에 대한 반응", body="\n".join(lines))
