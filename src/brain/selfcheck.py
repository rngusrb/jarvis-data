"""자기 점검 — 보낸 답을 스스로 돌아보고 남긴다(JAR-039), 모아서 부족한 기능을 말한다(JAR-040,
`/gaps`).

사용자 요청(2026-11-09): "자기가 잘 안 된 거에 대한 스스로 리포트 — 기능적으로 부족하다 느끼는
부분". 결정: "답은 기존처럼 내뱉고 나중에 저런 거 기록해 — 모델 호출에 부담은 없으니까". 그래서
답을 보낸 뒤 따로 한 번 묻는다 — 답을 쓰는 차례에 끼워 넣지 않는다(답이 늦어지거나 꼴이 바뀌지
않게).

판단은 정해진 꼴로만 받는다(brain/_GUIDE "판단을 자유 글로 받지 않는다") — 깨지면 적지 않고 원문을
경고로 남긴다. 갈래가 정해져 있어야 `/gaps` 가 센다.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.brain.client import Reasoner
from src.brain.tools import Use
from src.storage.selfchecks import SelfCheck, SQLiteSelfChecks

logger = logging.getLogger(__name__)

DONE, PARTLY, FAILED = "다 함", "일부", "못 함"
RESULTS = (DONE, PARTLY, FAILED)
NONE = "없음"
UNUSED = "있는 기능을 안 씀"
TIME = "도구 한도·시간"
OTHER = "기타"
BLOCKED = (
    NONE,
    UNUSED,
    "검색 결과가 모자람",
    "페이지를 못 읽음",
    "브라우저로도 못 찾음",
    "로그인이 필요함",
    "파일을 못 읽음",
    TIME,
    "내 기록에 없음",
    "할 수 없는 일",
    OTHER,
)
_TOOLS = {"search": "검색", "read": "읽기", "browse": "브라우저"}
# 돌아볼 때 보여주는 답의 길이(자).
ANSWER_SHOWN = 1500
# 답할 때 본 것(기록·최근 대화)의 상한(자). 넘으면 앞(기록)과 끝(지금 질문)을 남기고 가운데를
# 줄인다.
SEEN_CAP = 30_000

SYSTEM_PROMPT = f"""너는 자비스다. 방금 사용자에게 보낸 네 답을 스스로 돌아본다 — 물은 것을 다
해냈나, 못 했다면 무엇에 막혔나, 어떤 기능이 있었으면 해냈을까.

근거는 넷이다: [지금 있는 기능], [답할 때 본 것](기록과 최근 대화 — 답을 쓸 때 본 그대로),
[쓴 도구], [보낸 답]. [답할 때 본 것]의 기록으로 맞게 답했으면 근거가 있는 답이고 "{DONE}"이다.
답에 "못 찾았어", "~에는 없어", "확인 못 했어" 같은 말이 있으면 막힌 것이다. [답할 때 본 것]에도
[쓴 도구]에도 없는 바깥의 바뀌는 사실(일정·가격·뉴스)을 말했으면 "{PARTLY}"다. 쓴 도구에 그 시도가
없으면 못 한 게 아니라 안 한 것이다 — 예: 첨부를 못 열었다는데 그 첨부를 읽은 기록이 없다. 있는
기능으로 할 수 있었는데 안 썼으면 blocked 는 "{UNUSED}"이고 missing 은 빈 글이다 — 새 기능이
아니다. 있었으면 할 기능은 정당한 것만 쓴다 — 로그인을 뚫거나 막힌 것을 우회하는 것은 쓰지
않는다(로그인이 필요하면 "로그인해서 보기"). 한국어로만 쓴다.

JSON 하나로만 답한다:
{{"result": "{DONE}" | "{PARTLY}" | "{FAILED}", "blocked": "<갈래 하나>",
 "note": "무엇이 모자랐나 한 줄(다 했으면 빈 글)",
 "missing": "있었으면 해냈을 기능 한 줄(없으면 빈 글)"}}

갈래(blocked): {" · ".join(BLOCKED)}
다 했으면 result 는 "{DONE}", blocked 는 "{NONE}". 다 하지 못했으면 blocked 는 "{NONE}"이 아니다."""

GAPS_PROMPT = f"""아래는 자비스가 스스로 돌아본, 잘 안 된 답들이다. 같은 까닭끼리 묶어 부족한
기능을 많아야 셋 써라. 각각 한 줄: 무엇이 모자랐나(몇 번, 짧은 예 하나) — 무엇을 만들면 나아지나.
"{UNUSED}"은 새 기능이 아니라 쓰는 법(지시문)을 고칠 일로 쓴다. 로그인을 뚫거나 막힌 것을 우회하는
기능은 쓰지 않는다. 먼저 [지금 있는 기능]을 읽고, 거기 있는 것은 새로 만들 것으로 쓰지 않는다
— 예: 첨부·PDF 읽기가 있으면 "첨부 읽기 기능 추가"가 아니라 "첨부를 안 열었다(쓰는 법)"로 쓴다.
몇 번은 [센 것]의 수를 쓴다(아래 예는 갈래마다 몇 개만 골랐다). 기록에 없는 것은 지어내지 않는다.
머리말 없이 목록만, 반말로, 한국어로만."""
# `/gaps` 가 보는 기간, 정리에 넘기는 점검 수(갈래마다 고루), 정리의 상한(자), 보여주는 갈래 수.
GAP_DAYS = 14
GAP_ROWS = 30
WRITE_CAP = 2000
KINDS_SHOWN = 5


@dataclass
class SelfReview:
    reasoner: Reasoner
    store: SQLiteSelfChecks

    async def review(
        self,
        question: str,
        answer: str,
        uses: Sequence[Use],
        at: datetime,
        abilities: Sequence[str] = (),
        seen: str = "",
    ) -> Optional[SelfCheck]:
        """보낸 답을 돌아보고 남긴다. 못 읽은 판단은 남기지 않는다(None).

        `abilities` 는 지금 있는 기능(도구 이름: 설명) — 모르면 이미 있는 첨부 읽기를 "있었으면 할
        기능"으로 적었다(2026-11-09 실측). `seen` 은 답할 때 본 것(기록·최근 대화) 그대로 — 없으면
        내 기록으로 맞게 한 답을 "지어냄·내 기록에 없음"으로 판정했다(1차 리뷰 실측 12/12).
        """
        able = "\n".join(f"- {a}" for a in abilities) or "(도구 없음 — 아는 것과 기록으로만 답한다)"
        prompt = (
            f"[지금 있는 기능]\n{able}\n\n[답할 때 본 것]\n{_clip(seen) or '(없음)'}\n\n"
            f"[질문]\n{question}\n\n[쓴 도구]\n{_render_uses(uses)}"
            f"\n\n[보낸 답]\n{answer[:ANSWER_SHOWN]}"
        )
        reply = await self.reasoner.ask(prompt, system=SYSTEM_PROMPT)
        judged = parse_check(reply)
        if judged is None:
            logger.warning("자기 점검 판단을 못 읽었다(남기지 않는다): %.200s", reply)
            return None
        result, blocked, note, missing = judged
        check = SelfCheck(
            at=at,
            question=question,
            answer=answer,
            tools=_tools(uses),
            result=result,
            blocked=blocked,
            missing=missing,
            note=note,
        )
        self.store.add(check)
        if result != DONE:
            logger.info("자기 점검 — %s(%s): %s", result, blocked, note[:120])
        return check

    def failed(
        self, question: str, why: str, blocked: str, at: datetime, uses: Sequence[Use] = ()
    ) -> None:
        """모델을 부를 것도 없이 못 한 답(시간 초과·빈 답) — 바로 남긴다."""
        if blocked not in BLOCKED:
            raise ValueError(f"모르는 갈래: {blocked}")
        self.store.add(
            SelfCheck(
                at=at,
                question=question,
                answer="",
                tools=_tools(uses),
                result=FAILED,
                blocked=blocked,
                missing="",
                note=why,
            )
        )
        logger.info("자기 점검 — %s(%s): %s", FAILED, blocked, why)


@dataclass
class GapReport:
    reasoner: Reasoner
    store: SQLiteSelfChecks
    days: int = GAP_DAYS

    def tally(self, now: datetime) -> Tuple[str, List[SelfCheck]]:
        """센 것(모델 없이)과 막힌 점검들. 막힌 게 없으면 목록이 비고 센 것이 곧 답이다."""
        rows = self.store.since(now - timedelta(days=self.days))
        if not rows:
            return f"최근 {self.days}일 동안 돌아본 답이 없어.", []
        counts = Counter(r.result for r in rows)
        head = (
            f"최근 {self.days}일 내 답 {len(rows)}개를 돌아봤어 — {DONE} {counts[DONE]}"
            f" · {PARTLY} {counts[PARTLY]} · {FAILED} {counts[FAILED]}."
        )
        problems = [r for r in rows if r.result != DONE]
        if not problems:
            return f"{head}\n막힌 답은 없었어.", []
        lines = [head, "", "막힌 곳:"]
        for blocked, n in Counter(r.blocked for r in problems).most_common(KINDS_SHOWN):
            last = next(r for r in reversed(problems) if r.blocked == blocked)
            lines.append(f"- {blocked} {n}번 — 예: “{_short(last.question, 40)}”")
        return "\n".join(lines), problems

    async def write(
        self, tally: str, problems: Sequence[SelfCheck], abilities: Sequence[str] = ()
    ) -> str:
        """센 것 + 모델이 묶어 쓴 부족한 기능. 정리가 실패해도 센 것은 보낸다.

        `abilities` 는 지금 있는 기능 — 모르면 이미 있는 문서 읽기를 "만들 것"으로 썼다(2026-11-09
        실측).
        """
        # 갈래마다 고루, 최근 것부터 — 최근 30개만 넘겼더니 가장 많은 갈래(오래된 로그인 25번)를
        # 빼고 썼다(1차 리뷰 재현). 몇 번인지는 센 것을 같이 준다.
        groups: Dict[str, List[SelfCheck]] = {}
        for check in problems:
            groups.setdefault(check.blocked, []).append(check)
        order = sorted(groups, key=lambda blocked: -len(groups[blocked]))
        per = max(2, GAP_ROWS // max(1, len(groups)))
        rows = [check for blocked in order for check in groups[blocked][-per:]]
        able = "\n".join(f"- {a}" for a in abilities) or "(도구 없음)"
        counted = "\n".join(f"- {blocked} {len(groups[blocked])}번" for blocked in order)
        prompt = f"[지금 있는 기능]\n{able}\n\n[센 것]\n{counted}\n\n[예]\n" + "\n\n".join(
            f"질문: {_short(r.question, 120)}\n결과: {r.result}({r.blocked})\n"
            f"모자란 것: {r.note or '-'}\n있었으면 한 기능: {r.missing or '-'}"
            for r in rows
        )
        try:
            reply = await self.reasoner.ask(prompt, system=GAPS_PROMPT)
        except Exception:
            logger.exception("부족한 기능 정리를 못 했다 — 센 것만 보낸다(%d개)", len(rows))
            return f"{tally}\n\n(부족한 기능 정리는 못 했어 — 이유는 자비스 로그에)"
        if not reply or not reply.strip():
            logger.warning("부족한 기능 정리가 비었다(%d개)", len(rows))
            return f"{tally}\n\n(부족한 기능 정리가 비었어)"
        return f"{tally}\n\n부족한 기능:\n{reply.strip()[:WRITE_CAP]}"


def parse_check(reply: str) -> Optional[Tuple[str, str, str, str]]:
    """(result, blocked, note, missing). 꼴이 깨졌거나 모르는 값이면 None — 지어 채우지 않는다."""
    match = re.search(r"\{.*\}", (reply or "").strip(), re.DOTALL)
    if not match:
        return None
    try:
        data: Any = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    result, blocked = data.get("result"), data.get("blocked")
    if result not in RESULTS or blocked not in BLOCKED:
        return None
    if (result == DONE) != (blocked == NONE):
        # "일부"인데 막힌 곳이 없다거나, 다 했는데 막혔다 — 어긋난 판단은 판단이 아니다.
        return None
    note, missing = _line(data, "note"), _line(data, "missing")
    if note is None or missing is None:
        return None
    return str(result), str(blocked), note, missing


def _line(data: Dict[str, Any], key: str) -> Optional[str]:
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    return " ".join(value.split())


def _render_uses(uses: Sequence[Use]) -> str:
    if not uses:
        return "(도구를 안 썼다)"
    lines = []
    for use in uses:
        label = _TOOLS.get(use.kind, use.kind)
        outcome = use.note if use.ok else f"실패: {use.note}"
        lines.append(f"- {label} {_short(use.target, 160)} → {outcome}")
    return "\n".join(lines)


def _clip(seen: str) -> str:
    if len(seen) <= SEEN_CAP:
        return seen
    half = SEEN_CAP // 2
    logger.info("답할 때 본 것이 길어 가운데를 줄였다(%d자 → %d자)", len(seen), SEEN_CAP)
    return f"{seen[:half]}\n…(가운데 줄임)…\n{seen[-half:]}"


def _tools(uses: Sequence[Use]) -> Tuple[Tuple[str, str, bool, str], ...]:
    return tuple((u.kind, u.target, u.ok, u.note) for u in uses)


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
