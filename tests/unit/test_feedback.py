"""👍👎 — 버튼의 모양은 한 곳에서, 반응은 다음 판단의 맥락으로."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.brain.feedback import FeedbackProvider, buttons, new_token, parse
from src.brain.memory import InMemorySpeechLog

NOW = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)


def test_buttons_come_back_as_reaction_and_token() -> None:
    token = new_token()
    up, down = buttons(token)
    assert (up["text"], down["text"]) == ("👍", "👎")
    assert parse(up["action"]) == (1, token) and parse(down["action"]) == (-1, token)
    # 텔레그램 버튼 값은 64바이트까지다.
    assert all(len(b["action"].encode()) <= 64 for b in (up, down))


def test_other_buttons_are_not_reactions() -> None:
    assert parse("useless:sleep_drop") is None
    assert parse("fb:?:abc") is None
    assert parse("fb:+:") is None


def test_reactions_reach_the_next_judgment() -> None:
    log = InMemorySpeechLog()
    log.record("topic:3", NOW - timedelta(days=1), "요즘 자격증 보네", token="t1")
    log.record("late_sleep", NOW - timedelta(hours=2), "어젯밤 늦게 잤네", token="t2")
    log.rate("t1", -1)

    block = FeedbackProvider(log=log).fetch(None, NOW)

    assert block is not None and block.body == "- 11/04 👎 요즘 자격증 보네"
    assert FeedbackProvider(log=InMemorySpeechLog()).fetch(None, NOW) is None
