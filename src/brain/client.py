"""추론기 — vLLM(:8000) OpenAI 호환 API에 직접 붙는다.

LangGraph 에이전트(:8001)를 거치지 않는다. 자비스의 '에이전트'를 이 레포 안에
두기로 했기 때문이다. 맥락 조립과 판단은 src/brain이 하고, 저쪽은 토큰만 뱉는
역할로 쓴다. OpenAI 호환 규격이라 스키마를 추측할 필요가 없다는 것도 이점.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple

import httpx

logger = logging.getLogger(__name__)

# 일부 추론 모델은 답변 앞에 사고 과정을 <think>...</think>로 뱉는다.
REASONING_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """모델의 사고 과정 블록을 걷어낸다.

    이걸 안 하면 "음 사용자가 어젯밤 5시간 잤는데 이걸 알려야 하나..." 같은
    독백이 그대로 텔레그램으로 날아간다. 서버에 뜬 모델이 R1 distill이라
    이 처리는 선택이 아니라 필수다.
    """
    cleaned = REASONING_BLOCK.sub("", text)
    if "<think>" in cleaned:
        # 닫는 태그가 없다 = max_tokens에 걸려 사고 중에 잘렸다는 뜻.
        # 답변이 아직 안 나온 것이므로 앞부분만 남긴다(보통 빈 문자열).
        cleaned = cleaned.split("<think>")[0]
    return cleaned.strip()


def unreachable(error: BaseException) -> bool:
    """연결부터 안 된 실패인가 — 데스크탑이 꺼졌거나 길(Tailscale)이 끊겼다.

    서버가 답은 했는데 거절한 것(400 재료 초과, 5xx)이나 읽다가 한도가 찬 것과는 사용자에게 할 말이
    다르다. 앞은 "데스크탑이 켜져 있나 봐 줘", 뒤는 "조금 뒤에 다시". 거절에 앞의 말을 보내면 켜져
    있는 걸 보고 헷갈린다(JAR-027 재리뷰 재현 — vLLM 400 에 "닿지 않아"가 갔다).
    """
    return isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, ConnectionError))


@dataclass(frozen=True)
class ToolCall:
    """모델이 부른 도구 하나. `raw` 는 받은 모양 그대로 — 대화에 다시 실어 보낸다."""

    id: str
    name: str
    arguments: Dict[str, Any]
    raw: Dict[str, Any]


@dataclass(frozen=True)
class Turn:
    """도구를 줄 수 있는 대화의 한 차례. 도구를 불렀으면 `calls`, 아니면 `content` 가 답이다."""

    content: str
    calls: Tuple[ToolCall, ...] = ()
    finish: Optional[str] = None


class ToolReasoner(Protocol):
    """도구를 쓸 줄 아는 추론기. vLLM 을 `--enable-auto-tool-choice` 로 띄워야 한다(SETUP)."""

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        thinking: Optional[bool] = None,
    ) -> Turn: ...


class Reasoner(Protocol):
    """자비스가 필요로 하는 추론 능력의 전부.

    이것만 만족하면 무엇이든 두뇌로 꽂을 수 있다 — vLLM이든, 나중에
    도구가 필요해져서 :8001 에이전트로 돌아가든, 테스트용 가짜든.
    """

    async def ask(self, prompt: str, system: Optional[str] = None) -> str: ...


class VLLMClient:
    def __init__(
        self,
        base_url: str,
        model: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 120.0,
        temperature: float = 0.6,
        # 사고 과정에만 1000토큰을 쉽게 쓰는 모델들이 있다. 너무 짜게 주면
        # 답변까지 못 가고 잘려서 자비스가 영영 입을 안 여는 버그가 된다.
        max_tokens: int = 2048,
        # 시스템 프롬프트를 user 메시지에 접어넣는 우회. DeepSeek R1이
        # 시스템 프롬프트를 권장하지 않아서 넣었던 것으로, Qwen3에서는 불필요하다.
        fold_system: bool = False,
        # Judge가 하는 일은 "말할까 말까 + 한 문장"이고, 어려운 필터링(심각도·
        # 쿨다운)은 이미 게이트가 끝냈다. 남은 판단에 사고 1000토큰을 쓰는 건
        # 비싸서 기본은 끈다. 품질을 비교해보고 싶으면 켜면 된다.
        enable_thinking: bool = False,
        # 답의 꼴(JSON 스키마). 주면 `ask` 의 답이 이 꼴로만 나온다(vLLM 구조화 출력). 탐색
        # 판단이 값어치 칸에 제목을 쓰고, 사실을 늘어놓다 잘렸다(JAR-046 서버 실측 셋 중 둘).
        schema: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model or None
        self._schema = schema
        # 연결은 따로 짧게. 데스크탑이 꺼져 Tailscale 이 패킷을 버리면 연결 시도가 한도(보고서는
        # 10분)를 다 쓰고, 그동안 듣기 루프가 멈춘다(JAR-027 리뷰 재현). 긴 건 연결 뒤의 읽기다.
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
        self._temperature = temperature
        self._max_tokens = max_tokens
        # R1 계열은 시스템 프롬프트를 쓰지 말고 지시를 user 메시지에 넣으라고
        # 권고한다. 기본값을 True로 두되, 다른 모델로 바꾸면 끄면 된다.
        self._fold_system = fold_system
        self._enable_thinking = enable_thinking

    async def _resolve_model(self) -> str:
        """모델 이름을 모르면 서버에 물어본다.

        vLLM을 어떤 --served-model-name으로 띄웠는지 외울 필요가 없어진다.
        """
        if self._model:
            return self._model
        response = await self._client.get(f"{self._base_url}/v1/models")
        response.raise_for_status()
        data: Dict[str, Any] = response.json()
        items = data.get("data") or []
        if not items:
            raise RuntimeError(f"{self._base_url}에 서빙 중인 모델이 없다")
        model_id = str(items[0].get("id", "")).strip()
        if not model_id:
            raise RuntimeError("모델 목록에서 id를 찾지 못했다")
        self._model = model_id
        return model_id

    async def ask(self, prompt: str, system: Optional[str] = None) -> str:
        model = await self._resolve_model()

        messages: List[Dict[str, str]] = []
        user_content = prompt
        if system and self._fold_system:
            user_content = f"{system}\n\n{prompt}"
        elif system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if not self._enable_thinking:
            # Qwen3는 기본이 사고 켜짐이라 끌 때만 보낸다. 이 키를 모르는
            # 모델에 굳이 보내서 거절당할 이유가 없다.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if self._schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": self._schema},
            }

        response = await self._client.post(f"{self._base_url}/v1/chat/completions", json=payload)
        response.raise_for_status()
        data: Dict[str, Any] = response.json()

        choices = data.get("choices") or []
        if not choices:
            return ""

        choice = choices[0]
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            # --reasoning-parser를 붙인 서버는 사고 과정을 message.reasoning으로
            # 분리하고, 사고 중에 max_tokens에 걸리면 content를 **null**로 준다.
            # 이때 str(None)을 하면 "None"이라는 문자열이 만들어져 그대로
            # 사용자에게 발송된다. 조용히 넘어가는 편이 맞다.
            logger.warning(
                "모델이 답변을 내지 못했다 (finish_reason=%s). max_tokens가 부족할 수 있다.",
                choice.get("finish_reason"),
            )
            return ""

        if choice.get("finish_reason") == "length":
            # 답을 쓰다가 max_tokens 에 걸렸다. 내용은 돌려주되(부르는 쪽이 온전한 앞부분을 건질
            # 수 있다) 잘렸다는 건 남긴다. 안 남기면 부르는 쪽엔 "형식이 깨졌다"로만 보인다 —
            # 회고 되감기 87일 중 3일이 "JSON 을 못 찾았다"로 실패했는데 원인은 이 한도였다(11-02).
            usage = data.get("usage") or {}
            logger.warning(
                "모델 답이 max_tokens(%d)에서 잘렸다 — 받은 %d자, 답 토큰 %s",
                self._max_tokens,
                len(content),
                usage.get("completion_tokens"),
            )

        # reasoning-parser가 없는 서버를 대비한 안전망. 파서가 붙어 있으면 no-op이다.
        return strip_reasoning(content)

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        thinking: Optional[bool] = None,
    ) -> Turn:
        """메시지 목록으로 한 차례. 도구를 주면 모델이 부를지 정한다(`tool_choice=auto`).

        `thinking` 을 안 주면 이 클라이언트의 기본값을 따른다. 2026-11-05 실측: 이 모델은 생각을
        꺼도 도구를 제대로 부른다.
        """
        model = await self._resolve_model()
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if not (self._enable_thinking if thinking is None else thinking):
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        response = await self._client.post(f"{self._base_url}/v1/chat/completions", json=payload)
        response.raise_for_status()
        data: Dict[str, Any] = response.json()
        choices = data.get("choices") or []
        if not choices:
            return Turn(content="")
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content")
        text = strip_reasoning(content) if isinstance(content, str) else ""
        calls = tuple(_call(raw) for raw in message.get("tool_calls") or [])
        if choice.get("finish_reason") == "length":
            logger.warning(
                "모델 답이 max_tokens(%d)에서 잘렸다 — 받은 %d자, 도구 %d개",
                self._max_tokens,
                len(text),
                len(calls),
            )
        return Turn(content=text, calls=calls, finish=choice.get("finish_reason"))

    async def aclose(self) -> None:
        await self._client.aclose()


def _call(raw: Dict[str, Any]) -> ToolCall:
    """vLLM 이 준 도구 호출 하나.

    인자가 JSON 이 아니면 빈 인자로 두고 남긴다 — 도구가 "인자가 비었다"로 모델에게 돌려준다(조용히
    버리면 모델은 왜 답이 없는지 모른다).
    """
    function = raw.get("function") or {}
    name = str(function.get("name") or "")
    text = function.get("arguments") or "{}"
    try:
        arguments = json.loads(text) if isinstance(text, str) else dict(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("도구 인자가 JSON 이 아니다(%s): %.200s", name, text)
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return ToolCall(id=str(raw.get("id") or ""), name=name, arguments=arguments, raw=raw)
