"""추론기 — vLLM(:8000) OpenAI 호환 API에 직접 붙는다.

LangGraph 에이전트(:8001)를 거치지 않는다. 자비스의 '에이전트'를 이 레포 안에
두기로 했기 때문이다. 맥락 조립과 판단은 src/brain이 하고, 저쪽은 토큰만 뱉는
역할로 쓴다. OpenAI 호환 규격이라 스키마를 추측할 필요가 없다는 것도 이점.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Protocol

import httpx

# DeepSeek R1 계열은 답변 앞에 사고 과정을 <think>...</think>로 뱉는다.
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
        # R1 계열은 사고 과정에만 1000토큰을 쉽게 쓴다. 너무 짜게 주면
        # 답변까지 못 가고 잘려서 자비스가 영영 입을 안 여는 버그가 된다.
        max_tokens: int = 2048,
        fold_system: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model or None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._temperature = temperature
        self._max_tokens = max_tokens
        # R1 계열은 시스템 프롬프트를 쓰지 말고 지시를 user 메시지에 넣으라고
        # 권고한다. 기본값을 True로 두되, 다른 모델로 바꾸면 끄면 된다.
        self._fold_system = fold_system

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

        response = await self._client.post(
            f"{self._base_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "temperature": self._temperature,
                "max_tokens": self._max_tokens,
            },
        )
        response.raise_for_status()
        data: Dict[str, Any] = response.json()

        choices = data.get("choices") or []
        if not choices:
            return ""
        content = choices[0].get("message", {}).get("content", "")
        return strip_reasoning(str(content))

    async def aclose(self) -> None:
        await self._client.aclose()
