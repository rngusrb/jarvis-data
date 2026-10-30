"""로그에서 비밀값을 지운다.

httpx 는 INFO 로 요청 URL 을 통째로 찍는다. 텔레그램 API 는 봇 토큰을 URL 경로에
넣으므로(`/bot<토큰>/getUpdates`), 30초마다 도는 듣기 루프가 journalctl 에 토큰을
하루 2,880번 남긴다. 2026-10-30 서버 로그를 보다가 확인했고, 그 로그는 디버깅하며
채팅에 그대로 붙여넣어졌다.

요청 로그를 끄는 대신 값만 지운다. "vLLM 을 몇 번 불렀나"는 사고를 풀 때 쓰인
기록이다(SKIP 미기록으로 68번 부른 사고).
"""

from __future__ import annotations

import logging
from typing import List, Optional


class HideSecrets(logging.Filter):
    """메시지에 든 비밀값을 *** 로 바꾼다. 레코드를 버리지는 않는다."""

    def __init__(self, secrets: List[str]) -> None:
        super().__init__()
        # 짧은 값은 지우지 않는다. 빈 문자열이나 "1" 같은 값이 섞이면 로그 전체가 망가진다.
        self.secrets = [s for s in secrets if len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        hidden = message
        for secret in self.secrets:
            hidden = hidden.replace(secret, "***")
        if hidden != message:
            record.msg, record.args = hidden, ()
        return True


def hide_in_logs(*secrets: Optional[str], logger: Optional[logging.Logger] = None) -> None:
    """루트 로거의 처리기마다 거름망을 단다. 다른 로거에서 올라온 기록도 여기를 지난다."""
    target = logger or logging.getLogger()
    for handler in target.handlers:
        handler.addFilter(HideSecrets([s for s in secrets if s]))
