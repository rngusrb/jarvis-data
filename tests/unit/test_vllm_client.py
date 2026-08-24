from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import httpx

from src.brain.client import VLLMClient

MODELS = {"data": [{"id": "Qwen/Qwen3.8-27B-FP8"}]}


def _client(reply: Dict[str, Any], sent: List[Dict[str, Any]] | None = None) -> VLLMClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json=MODELS)
        if sent is not None:
            import json

            sent.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    transport = httpx.MockTransport(handler)
    return VLLMClient("http://vllm", client=httpx.AsyncClient(transport=transport))


def test_사고하다_잘리면_입을_다문다() -> None:
    """reasoning-parser가 붙은 서버는 이때 content를 null로 준다.

    str(None)을 하면 "None"이라는 문자열이 그대로 사용자에게 발송된다.
    실제 vLLM 응답으로 재현한 케이스다.
    """
    reply = {
        "choices": [{"finish_reason": "length", "message": {"content": None, "reasoning": "음..."}}]
    }
    assert asyncio.run(_client(reply).ask("안녕")) == ""


def test_정상_응답은_그대로_돌려준다() -> None:
    reply = {"choices": [{"finish_reason": "stop", "message": {"content": "  잘 잤어?  "}}]}
    assert asyncio.run(_client(reply).ask("안녕")) == "잘 잤어?"


def test_빈_문자열도_발화하지_않는다() -> None:
    reply = {"choices": [{"finish_reason": "stop", "message": {"content": "   "}}]}
    assert asyncio.run(_client(reply).ask("안녕")) == ""


def test_choices가_없으면_빈_문자열() -> None:
    assert asyncio.run(_client({"choices": []}).ask("안녕")) == ""


def test_파서_없는_서버의_think_블록도_걷어낸다() -> None:
    """--reasoning-parser를 안 붙인 서버를 대비한 안전망."""
    reply = {"choices": [{"message": {"content": "<think>고민</think>어젯밤 잘 못 잤네."}}]}
    assert asyncio.run(_client(reply).ask("안녕")) == "어젯밤 잘 못 잤네."


def test_모델_이름을_서버에_물어본다() -> None:
    sent: List[Dict[str, Any]] = []
    reply = {"choices": [{"message": {"content": "응"}}]}
    asyncio.run(_client(reply, sent).ask("안녕"))
    assert sent[0]["model"] == "Qwen/Qwen3.8-27B-FP8"


def test_기본은_사고를_끄고_보낸다() -> None:
    sent: List[Dict[str, Any]] = []
    reply = {"choices": [{"message": {"content": "응"}}]}
    asyncio.run(_client(reply, sent).ask("안녕"))
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_시스템_프롬프트를_접지_않는다() -> None:
    """Qwen3는 시스템 프롬프트를 정상 처리한다. 접어넣던 건 R1용 우회였다."""
    sent: List[Dict[str, Any]] = []
    reply = {"choices": [{"message": {"content": "응"}}]}
    asyncio.run(_client(reply, sent).ask("안녕", system="너는 비서다"))
    roles = [m["role"] for m in sent[0]["messages"]]
    assert roles == ["system", "user"]
