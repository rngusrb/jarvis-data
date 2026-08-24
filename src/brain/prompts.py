"""프롬프트 템플릿과 응답 파싱.

에이전트 로직에서 떼어놓은 이유는 이 파일이 제일 자주 바뀔 곳이기 때문이다.
말투를 고치려고 agent.py를 열게 되면 안 된다.
"""

from __future__ import annotations

from typing import Optional, Sequence

from src.brain.context import ContextBlock, render
from src.core.models import Insight

SKIP_TOKEN = "SKIP"

SYSTEM_PROMPT = f"""너는 사용자의 개인 비서다. 감지된 신호를 보고 지금 말을 걸지 정한다.

판단 기준:
- 사용자가 이미 알고 있을 법한 이야기면 말하지 않는다.
- 지금 당장 할 수 있는 행동이 없으면 말하지 않는다.
- 최근에 비슷한 말을 했으면 말하지 않는다.

말할 가치가 없으면 다른 말 없이 {SKIP_TOKEN} 한 단어만 출력한다.
말할 가치가 있으면 한국어 반말로 두 문장 이내로 말한다.
숫자를 나열하지 말고, 사용자가 지금 무엇을 하면 좋을지에 초점을 둔다."""


def build_prompt(insight: Insight, blocks: Sequence[ContextBlock]) -> str:
    parts = [f"감지된 신호: {insight.summary}", f"심각도: {insight.severity.name}"]
    if blocks:
        parts.append(f"참고 맥락:\n{render(blocks)}")
    parts.append(f"지금 말을 걸까? 아니면 {SKIP_TOKEN}?")
    return "\n\n".join(parts)


def parse_decision(reply: str) -> Optional[str]:
    """모델 응답에서 최종 발화를 뽑는다. 말 안 걸기로 했으면 None."""
    text = reply.strip()
    if not text:
        return None
    if text.upper().startswith(SKIP_TOKEN):
        return None
    return text
