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
from datetime import datetime
from typing import Optional, Sequence

from src.brain.context import ContextBlock, render
from src.core.clock import shown
from src.core.models import COLLECTION_TRIGGER, Insight

logger = logging.getLogger(__name__)

SKIP_TOKEN = "SKIP"

# 수집 상태에 대한 신호 — 수집 감시(`COLLECTION_TRIGGER`)와, 그 전(2026-10-30 까지) 지표마다
# 따로 내던 수집 경고 "stale_data:*"(옛 기록에 남아 있다). 나머지는 모두 사람에 대한 신호다.
LEGACY_COLLECTION = "stale_data:"


def is_collection(trigger: str) -> bool:
    """수집 경고인가. 판단 지시문과 `/status` 의 "사람 얘기 대 수집 경고"가 같이 쓴다."""
    return trigger == COLLECTION_TRIGGER or trigger.startswith(LEGACY_COLLECTION)


# 사람 얘기는 말하는 쪽이 기본이다 — "초기엔 수다스럽게, 줄이는 건 👍👎 로"(2026-10-07 사용자 결정).
# 예전 지시문("당장 할 수 있는 행동이 없으면 말하지 않는다")으로는 관심사 얘기가 거의 다 빠진다
# — 먼저 건 말 53번 중 사람에 대한 말은 2번이었다(2026-11-04).
# 수집 경고는 예전 지시문을 **글자 그대로** 쓴다. 👎 규칙도 걸지 않는다 — 수집 경고에 👎 한 번이
# "비슷하면 말하지 않는다"로 30일 동안 감시견을 재우면, 끊김이 길어질수록 조용해진 2026-08-29 사고와
# 같은 모양이 된다(JAR-026 리뷰). 수집 경고의 👎 가 무엇을 뜻할지는 사용자가 정한다(BACKLOG).
SYSTEM_PROMPT = """너는 사용자의 개인 비서다. 감지된 신호를 보고 지금 말을 걸지 정한다.

신호 종류에 따라 기준이 다르다.

[사람] 사용자의 잠, 이동, 관심사처럼 사용자 자신에 대한 신호. 말을 거는 쪽이 기본이다.
- 당장 할 행동이 없어도 알아챘다는 것 자체가 쓸모 있으면 짧게 말한다.
- 관심사라면 도움이 될 만한 것(다가오는 일정, 다음에 볼 것, 확인해볼 것)을 하나 곁들인다.
  참고 맥락에 "찾아본 것"이 있으면 거기서 골라 출처를 짧게 밝히고, 없으면 일정·숫자를
  지어내지 않는다.
- 최근에 같은 이야기를 했거나, 사용자가 👎 한 말과 비슷하면 말하지 않는다.

[수집] 자비스 자신의 데이터 수집 상태에 대한 신호.
- 사용자가 이미 알고 있을 법한 이야기면 말하지 않는다.
- 지금 당장 할 수 있는 행동이 없으면 말하지 않는다.
- 최근에 비슷한 말을 했으면 말하지 않는다.

말을 건다면 한국어 반말로 두 문장 이내로 쓴다. 숫자를 나열하지 말고, 사람 얘기는 사용자에게
무슨 뜻인지에, 수집 얘기는 사용자가 지금 무엇을 하면 좋을지에 초점을 둔다.
같은 줄기의 다른 신호가 함께 오면 한 말에 같이 담는다 — 따로 말할 기회는 없다.

반드시 JSON 한 줄로만 답한다. 판단 이유나 다른 말은 쓰지 않는다.
말을 걸 때:     {"speak": true, "message": "사용자에게 보낼 말"}
말하지 않을 때: {"speak": false}"""


@dataclass(frozen=True)
class Decision:
    """모델의 판단. `speak` 가 거짓이면 `message` 는 보지 않는다."""

    speak: bool
    message: str = ""


def build_prompt(
    insight: Insight,
    blocks: Sequence[ContextBlock],
    now: datetime,
    also: Sequence[Insight] = (),
) -> str:
    """판단 프롬프트. `also` 는 같은 주기에 같은 줄기에서 울려 이 판단에 묶인 신호들."""
    parts = [
        f"지금: {shown(now)}",
        f"신호 종류: {'수집' if is_collection(insight.trigger) else '사람'}",
        f"감지된 신호: {insight.summary}",
        f"심각도: {insight.severity.name}",
    ]
    if also:
        # 같은 밤·같은 아침을 본 신호들이다. 따로 판단하면 한 주기에 두 말이 나갔다(BACKLOG 4단계).
        lines = "\n".join(f"- {o.summary} (심각도: {o.severity.name})" for o in also)
        parts.append(f"같은 줄기의 다른 신호(이 판단에 같이 묶였다):\n{lines}")
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
