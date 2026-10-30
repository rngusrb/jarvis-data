"""시각의 두 가지 규칙 — 저장할 때와 보여줄 때.

**저장·비교**: 시각은 문자열로 저장되고 문자열로 비교된다. 흔적·발화 기록·믿음은 쓸 때
`stamp()` 로 UTC 표기를 맞춰 글자 비교가 곧 순간 비교가 되게 한다. 관측치만 예외다 —
폰이 보낸 `+09:00` 표기로 쌓인 옛 행이 있어 저장소가 `julianday()` 로 순간 비교한다.

**보여주기**: 사람과 LLM 에게는 `local()` 로 한국 시간을 보여준다.

보여줄 때 바꾸지 않으면 LLM 이 생활 리듬을 거꾸로 읽는다. 2026-10-30 까지 회고 프롬프트는
낮 12시 56분의 웹 방문을 "03:56"으로 보여주고 있었다 — 새벽에 깨어 있는 사람으로 보인다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")


def stamp(at: datetime) -> str:
    """저장하고 비교하는 시각 문자열. 시간대를 UTC 하나로 맞춘다.

    표기가 섞이면 같은 순간이 두 줄이 되고(흔적의 중복 판단은 시각 글자를 쓴다), 기간
    조회가 최대 9시간 어긋난다. 시간대 없는 값은 그대로 둔다 — 수신구가 받지 않으므로
    서버 코드가 만든 값뿐이다.
    """
    return at.astimezone(timezone.utc).isoformat() if at.tzinfo else at.isoformat()


def local(at: datetime) -> datetime:
    """보여줄 시각. 시간대 없는 값은 UTC 로 본다 — 서버가 만드는 시각은 전부 UTC 다."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(KST)
