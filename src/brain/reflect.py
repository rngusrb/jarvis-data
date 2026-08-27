"""회고 — 흔적을 훑어 믿음을 만든다.

자비스에게 두 번째 루프다. 첫 번째(`runtime/loop.py`)는 **반응형**이다 —
신호가 오면 30분 안에 판단하고 말을 건다. 이건 **회고형**이다. 주 단위로
쌓인 흔적 전체를 놓고 "이 사람 요즘 뭐 하고 있나"를 묻는다.

둘을 나눈 이유는 재료가 다르기 때문이다. "어젯밤 2.7시간 잤다"는 즉시
반응할 신호지만, "전세대출을 검색했다"는 아니다. 흔적 하나로는 아무 말도
할 수 없고, 서른 개가 모여야 "이사 준비 중"이 된다.

## 이름 난장판을 막는 장치

LLM에게 `kind` 를 자유롭게 만들게 하면 2주 만에 이렇게 된다.

    관심사:주거 / 관심사: 주거 / housing_interest / 이사 / 전세

그래서 **만들기 전에 기존 목록 전부를 보여준다.** 이 방식은 이미 확인됐다 —
같은 프롬프트 구조로 기능 제안을 시켰을 때 모델이 이미 있는 트리거를 다시
제안하지 않았고, 데이터가 부족한 영역은 아예 건너뛰었다.

## 근거를 강제하는 이유

같은 실험에서 모델은 7일 평균 두 개를 비교해놓고 "상관관계가 뚜렷하게
나타난다"고 썼다. **근거를 실제보다 세게 말하는 게 이 방식의 주된 실패
모양이다.** 흔적 텍스트를 그대로 인용하게 하면 지어내기가 훨씬 어려워진다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from src.brain.client import Reasoner
from src.core.beliefs import Belief
from src.core.models import Trace
from src.core.traces import TraceKind
from src.storage.beliefs import SQLiteBeliefStore
from src.storage.traces import SQLiteTraceStore

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """너는 한 사람의 개인 데이터를 관리하는 에이전트다.
그 사람이 남긴 흔적(검색, 파일, 앱 실행)을 보고 지금 무엇에 관심이 있는지 추론한다.

지켜야 할 것:
- 흔적에 실제로 있는 것만 쓴다. 일반적인 상식이나 추측으로 채우지 않는다.
- 근거로 쓴 흔적의 텍스트를 그대로 인용한다. 인용 못 하면 그 믿음은 만들지 않는다.
- 흔적 하나짜리는 관심사가 아니라 스쳐간 호기심이다. 만들지 않는다.
- 이미 있는 믿음과 같은 것이면 새로 만들지 말고 그 kind를 그대로 쓴다.

JSON 배열로만 답한다. 다른 말은 쓰지 않는다.

[
  {
    "kind": "관심사:주거",
    "value": "전세자금대출 조건을 알아보는 중",
    "confidence": 0.8,
    "evidence": ["전세자금대출 금리 비교", "버팀목 전세자금 자격"]
  }
]

찾은 게 없으면 빈 배열 [] 만 출력한다."""


@dataclass
class Reflection:
    """한 번의 회고 결과. 로그가 아니라 **되돌아볼 수 있는 기록**이다."""

    considered: int = 0
    learned: List[Belief] = field(default_factory=list)
    forgotten: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


@dataclass
class Reflector:
    reasoner: Reasoner
    traces: SQLiteTraceStore
    beliefs: SQLiteBeliefStore
    kinds: Sequence[TraceKind]
    # 한 번에 훑는 기간. 너무 길면 3주 전 관심사가 매번 다시 올라온다.
    lookback: timedelta = timedelta(days=7)
    # 종류별 흔적 상한. 프롬프트가 터지는 걸 막는다.
    per_kind_limit: int = 120

    async def run_once(self, now: datetime) -> Reflection:
        result = Reflection()

        # 먼저 잊는다. 시든 믿음을 프롬프트에 넣으면 모델이 그걸 근거로
        # 새 믿음을 지어낸다 — 죽은 관심사가 스스로 되살아나는 고리가 된다.
        result.forgotten = self.beliefs.forget_stale(now)

        harvest = self._gather(now)
        result.considered = sum(len(v) for v in harvest.values())
        if not harvest:
            logger.info("회고 — 훑을 흔적이 없다")
            return result

        prompt = self._build_prompt(harvest, now)
        reply = await self.reasoner.ask(prompt, system=SYSTEM_PROMPT)

        for item in _parse(reply):
            belief = self._to_belief(item, now, result)
            if belief is not None:
                result.learned.append(self.beliefs.observe(belief, now))

        logger.info(
            "회고 — 흔적 %d개, 믿음 %d건 갱신, %d건 망각, %d건 기각",
            result.considered,
            len(result.learned),
            len(result.forgotten),
            len(result.skipped),
        )
        return result

    def _gather(self, now: datetime) -> Dict[TraceKind, List[Trace]]:
        since = now - self.lookback
        found: Dict[TraceKind, List[Trace]] = {}
        for card in self.kinds:
            if not card.active:
                continue
            traces = list(self.traces.recent(card.kind, since, limit=self.per_kind_limit))
            if traces:
                found[card] = traces
        return found

    def _build_prompt(self, harvest: Dict[TraceKind, List[Trace]], now: datetime) -> str:
        parts: List[str] = []
        for card, traces in harvest.items():
            lines = "\n".join(f"  {t.at:%m-%d %H:%M}  {t.text}" for t in traces)
            parts.append(f"[{card.label}] {len(traces)}건\n{lines}")

        # 이름 난장판을 막는 자리. 기존 목록을 보여주지 않으면 같은 걸
        # 매번 새 이름으로 만든다.
        known = self.beliefs.all()
        if known:
            existing = "\n".join(f"  {b.kind} = {b.value} (확신 {b.confidence:.1f})" for b in known)
            parts.append(f"[이미 알고 있는 것]\n{existing}\n\n같은 것이면 위 kind를 그대로 써라.")
        else:
            parts.append("[이미 알고 있는 것]\n  (없음)")

        parts.append(f"오늘은 {now:%Y-%m-%d} 이다. 이 사람은 지금 무엇에 관심이 있나?")
        return "\n\n".join(parts)

    def _to_belief(
        self, item: Dict[str, Any], now: datetime, result: Reflection
    ) -> Optional[Belief]:
        kind = str(item.get("kind", "")).strip()
        value = str(item.get("value", "")).strip()
        evidence = tuple(str(e).strip() for e in item.get("evidence", []) if str(e).strip())

        if not kind or not value:
            result.skipped.append(f"kind나 value가 비었다: {item!r:.80}")
            return None
        # 근거 없는 믿음은 검증할 수 없다. 모델이 그럴듯한 문장을 지어낼 때
        # 제일 먼저 빠뜨리는 게 이 칸이라, 여기서 걸러진다.
        if len(evidence) < 2:
            result.skipped.append(f"{kind}: 근거가 {len(evidence)}개뿐")
            return None

        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5

        try:
            return Belief(
                kind=kind,
                value=value,
                confidence=min(max(confidence, 0.0), 1.0),
                first_seen=now,
                last_seen=now,
                evidence=evidence,
            )
        except ValueError as exc:
            result.skipped.append(f"{kind}: {exc}")
            return None


def _parse(reply: str) -> List[Dict[str, Any]]:
    """모델 응답에서 JSON 배열을 뽑는다.

    코드펜스로 감싸거나 앞에 설명을 붙이는 모델이 흔하다. 그때마다 회고가
    통째로 실패하는 대신 배열만 도려낸다 — 완벽한 형식을 요구하는 것보다
    관대하게 읽는 쪽이 실제로 잘 돈다.
    """
    text = reply.strip()
    if not text:
        return []
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        logger.warning("회고 응답에서 JSON 배열을 못 찾았다: %.200s", text)
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.warning("회고 응답 JSON 파싱 실패: %.200s", match.group(0))
        return []
    return [item for item in parsed if isinstance(item, dict)]
