"""로그 비밀값 지우기."""

from __future__ import annotations

import io
import logging

from src.core.logs import hide_in_logs

TOKEN = "1234567890:FAKE-token-only-for-this-test"


def _logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    out = io.StringIO()
    root = logging.getLogger(f"test-root-{name}")
    root.handlers = [logging.StreamHandler(out)]
    root.propagate = False
    root.setLevel(logging.INFO)
    return root, out


def test_다른_로거에서_올라온_URL_의_토큰도_지운다() -> None:
    root, out = _logger("url")
    hide_in_logs(TOKEN, logger=root)
    child = logging.getLogger("test-root-url.httpx")

    child.info('HTTP Request: POST https://api.telegram.org/bot%s/getUpdates "200 OK"', TOKEN)

    assert TOKEN not in out.getvalue()
    assert "bot***/getUpdates" in out.getvalue()


def test_비밀값이_없는_기록은_그대로_둔다() -> None:
    root, out = _logger("plain")
    hide_in_logs(TOKEN, "", None, logger=root)

    root.info("주기 완료 — %d건 발송", 1)

    assert out.getvalue().strip() == "주기 완료 — 1건 발송"
