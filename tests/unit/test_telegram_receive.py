"""텔레그램 수신 — 주인 검증과 커서.

여기서 지키는 건 하나다. **봇 사용자명은 공개라 아무나 말을 걸 수 있다.**
거르지 않으면 남이 주인의 수면·위치를 물어볼 수 있다.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import httpx

from src.channels.telegram import TelegramChannel

OWNER = "8718069067"
STRANGER = "99999999"


def _channel(updates: List[Dict[str, Any]]) -> TelegramChannel:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": updates})

    transport = httpx.MockTransport(handler)
    return TelegramChannel(
        bot_token="t",
        chat_id=OWNER,
        client=httpx.AsyncClient(transport=transport),
        poll_seconds=0,
    )


def _message(update_id: int, chat_id: str, text: str) -> Dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"date": 1790000000, "chat": {"id": int(chat_id)}, "text": text},
    }


def test_주인_메시지는_받는다() -> None:
    got = asyncio.run(_channel([_message(1, OWNER, "요즘 수면 어때?")]).receive(0))
    assert [i.text for i in got] == ["요즘 수면 어때?"]
    assert got[0].cursor == 1


def test_남의_메시지는_버린다() -> None:
    """봇 사용자명이 공개라 누구나 말을 걸 수 있다."""
    updates = [_message(1, STRANGER, "쟤 어제 몇시간 잤어?"), _message(2, OWNER, "안녕")]
    got = asyncio.run(_channel(updates).receive(0))
    assert [i.text for i in got] == ["안녕"]


def test_버튼_누름을_읽는다() -> None:
    update = {
        "update_id": 7,
        "callback_query": {
            "id": "cb1",
            "data": "useless:sleep_drop",
            "message": {"date": 1790000000, "chat": {"id": int(OWNER)}, "text": "어젯밤 짧았어"},
        },
    }
    got = asyncio.run(_channel([update]).receive(0))
    assert got[0].is_action
    assert got[0].action == "useless:sleep_drop"
    assert got[0].meta["callback_id"] == "cb1"


def test_남이_누른_버튼도_버린다() -> None:
    update = {
        "update_id": 7,
        "callback_query": {
            "id": "cb1",
            "data": "useless:sleep_drop",
            "message": {"date": 1790000000, "chat": {"id": int(STRANGER)}, "text": "x"},
        },
    }
    assert asyncio.run(_channel([update]).receive(0)) == []


def test_텍스트가_없는_것도_커서는_넘긴다() -> None:
    """사진·스티커는 다룰 줄 모르지만, 건너뛰지 않으면 영원히 다시 읽는다."""
    update = {
        "update_id": 5,
        "message": {"date": 1790000000, "chat": {"id": int(OWNER)}, "photo": [{"file_id": "x"}]},
    }
    got = asyncio.run(_channel([update]).receive(0))
    assert len(got) == 1
    assert got[0].text == ""
    assert got[0].cursor == 5
