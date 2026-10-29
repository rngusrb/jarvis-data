"""프롬프트 템플릿과 응답 파싱.

에이전트 로직에서 떼어놓은 이유는 이 파일이 제일 자주 바뀔 곳이기 때문이다.
말투를 고치려고 agent.py를 열게 되면 안 된다.

## 판단은 형식으로 받는다

"말할지"와 "할 말"을 한 덩어리 자유 글로 받으면, 모델이 판단 과정을 문장으로
풀어 쓴 것이 그대로 사용자에게 간다.

사고 이력: 2026-10-15 아침 "어제 밤에 이미 아이폰 자동화 점검을 요청했으니 오늘은
중복 알림을 보낸다"가 텔레그램으로 나갔다. 말하지 않겠다는 판단을 `SKIP` 한 단어
대신 문장으로 썼고, 코드는 `SKIP` 으로 시작하지 않으니 할 말로 처리했다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from src.brain.context import ContextBlock, render
from src.core.models import Insight

logger = logging.getLogger(__name__)

SKIP_TOKEN = "SKIP"

SYSTEM_PROMPT = """너는 사용자의 개인 비서다. 감지된 신호를 보고 지금 말을 걸지 정한다.

판단 기준:
- 사용자가 이미 알고 있을 법한 이야기면 말하지 않는다.
- 지금 당장 할 수 있는 행동이 없으면 말하지 않는다.
- 최근에 비슷한 말을 했으면 말하지 않는다.

말을 건다면 한국어 반말로 두 문장 이내로 쓴다. 숫자를 나열하지 말고, 사용자가 지금
무엇을 하면 좋을지에 초점을 둔다.

반드시 JSON 한 줄로만 답한다. 판단 이유나 다른 말은 쓰지 않는다.
말을 걸 때:     {"speak": true, "message": "사용자에게 보낼 말"}
말하지 않을 때: {"speak": false}"""


@dataclass(frozen=True)
class Decision:
    """모델의 판단. `speak` 가 거짓이면 `message` 는 보지 않는다."""

    speak: bool
    message: str = ""


def build_prompt(insight: Insight, blocks: Sequence[ContextBlock]) -> str:
    parts = [f"감지된 신호: {insight.summary}", f"심각도: {insight.severity.name}"]
    if blocks:
        parts.append(f"참고 맥락:\n{render(blocks)}")
    parts.append("지금 말을 걸까?")
    return "\n\n".join(parts)


def parse_decision(reply: str) -> Optional[Decision]:
    """모델 응답을 판단으로 읽는다. 형식이 깨졌으면 None.

    None 은 "말하지 않기로 했다"가 아니라 "무슨 판단인지 모른다"다. 둘을 섞으면
    안 된다 — 말하지 않기로 한 건 기록해서 쿨다운을 소모하고, 모르는 건 기록하지
    않고 다음 주기에 다시 묻는다.

    `SKIP` 한 단어는 예전 형식이지만 뜻이 분명해서 "말하지 않음"으로 받는다.
    """
    text = (reply or "").strip()
    if text.upper() == SKIP_TOKEN:
        return Decision(speak=False)

    # 코드펜스나 앞뒤 군말이 붙는 일이 흔하다. 객체 하나만 도려낸다.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("speak"), bool):
        return None

    if not data["speak"]:
        return Decision(speak=False)
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        # 말하겠다면서 할 말이 비었다. 빈 메시지를 보내는 것도, 말 안 한 걸로
        # 치는 것도 틀리다 — 판단이 깨진 것이다.
        return None
    return Decision(speak=True, message=message.strip())
