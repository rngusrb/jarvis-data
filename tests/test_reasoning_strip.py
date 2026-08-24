from __future__ import annotations

from src.brain.client import strip_reasoning


def test_사고과정_블록을_걷어낸다() -> None:
    raw = "<think>음 이걸 알려야 하나 고민되네</think>어젯밤 좀 못 잤네. 오늘은 일찍 자자."
    assert strip_reasoning(raw) == "어젯밤 좀 못 잤네. 오늘은 일찍 자자."


def test_여러_블록도_전부_걷어낸다() -> None:
    raw = "<think>하나</think>말이야 <think>둘</think>그렇다고"
    assert strip_reasoning(raw) == "말이야 그렇다고"


def test_사고_중에_잘리면_빈_문자열이_된다() -> None:
    """max_tokens에 걸려 답변까지 못 간 경우 — 독백을 사용자에게 보내면 안 된다."""
    raw = "<think>음 이걸 알려야 하나 계속 고민중인데"
    assert strip_reasoning(raw) == ""


def test_블록이_없으면_그대로_둔다() -> None:
    assert strip_reasoning("  그냥 답변  ") == "그냥 답변"
