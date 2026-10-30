"""시각을 이 사람의 시간대로.

저장소는 시각을 받은 표기 그대로 둔다 — 관측치는 폰이 보낸 `+09:00`, 흔적과 발화 기록은
UTC 다. 비교는 저장소가 순간으로 하고(표기가 달라도 같은 순간은 같다), 사람과 LLM 에게
보여줄 때는 여기서 한국 시간으로 바꾼다.

보여줄 때 바꾸지 않으면 LLM 이 생활 리듬을 거꾸로 읽는다. 2026-10-30 까지 회고 프롬프트는
낮 12시 56분의 웹 방문을 "03:56"으로 보여주고 있었다 — 새벽에 깨어 있는 사람으로 보인다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")


def local(at: datetime) -> datetime:
    """보여줄 시각. 시간대 없는 값은 UTC 로 본다 — 서버가 만드는 시각은 전부 UTC 다."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(KST)
