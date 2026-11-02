"""회고 — 흔적을 훑어 주제 나무를 가꾼다.

자비스에게 두 번째 루프다. 첫 번째(`runtime/loop.py`)는 **반응형**이다 —
신호가 오면 30분 안에 판단하고 말을 건다. 이건 **회고형**이다. 하루에 한 번
쌓인 흔적을 놓고 "이 사람 요즘 뭐 하고 있나"를 묻는다.

둘을 나눈 이유는 재료가 다르기 때문이다. "어젯밤 2.7시간 잤다"는 즉시
반응할 신호지만, "전세대출을 검색했다"는 아니다. 흔적 하나로는 아무 말도
할 수 없고, 서른 개가 모여야 "이사 준비 중"이 된다.

## 모델은 연산을 내고, 코드가 검사한다

모델에게 나무를 통째로 다시 쓰게 하지 않는다. 지금 나무와 아직 아무 주제에도 안 붙은 흔적을
보여주고 **바꿀 것만** 연산으로 받는다(`core.topics.plan` 이 검사). 통째로 다시 쓰게 하면
매일 조금씩 다른 나무가 나와 무엇이 실제로 바뀌었는지 알 수 없다.

## 이름 난장판을 막는 장치

자유롭게 이름을 짓게 하면 2주 만에 이렇게 된다.

    관심사:주거 / 관심사: 주거 / housing_interest / 이사 / 전세

그래서 **만들기 전에 나무 전부를 보여준다.** 잠잠한 주제까지 보여준다 — 다시 보인 관심사를
새 이름으로 또 만들지 않게.

## 근거를 강제하는 이유

같은 실험에서 모델은 7일 평균 두 개를 비교해놓고 "상관관계가 뚜렷하게
나타난다"고 썼다. **근거를 실제보다 세게 말하는 게 이 방식의 주된 실패
모양이다.** 근거는 흔적 번호로만 받고, 보여준 번호가 아니면 코드가 뺀다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from src.brain.client import Reasoner
from src.core.clock import local, shown
from src.core.models import Trace
from src.core.topics import Op, Ref, Standing, Trend, describe, plan, stale, standings
from src.core.traces import TraceKind
from src.storage.topics import Note, SQLiteTopicStore
from src.storage.traces import SQLiteTraceStore

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """너는 한 사람의 개인 데이터를 관리하는 에이전트다.
그 사람이 남긴 흔적(웹 방문·검색, 앱 사용)을 보고 이 사람이 요즘 무엇에 관심이 있고
어떻게 지내는지를 **주제 나무**로 가꾼다. 큰 줄기(예: 취업) 아래 가지(예: 자소서, 코딩테스트)를
둔다. 단계 수는 정해져 있지 않다.

할 수 있는 연산:
  create  새 주제. 서로 다른 흔적 2개 이상이 근거여야 한다. 하나뿐이면 스쳐간 호기심이다
  attach  있는 주제에 새 근거를 붙인다
  note    주제에 대해 알게 된 것을 적는다. 시기, 반복, 다른 주제와의 연결까지 자유롭게, 필요한 만큼
  rename  이름을 바꾼다
  move    다른 주제 아래로 옮긴다. parent 가 null 이면 큰 줄기가 된다
  merge   같은 것을 가리키는 두 주제를 합친다(topic 을 into 로)
  retire  더 이상 의미 없는 주제를 접는다. 아래에 살아 있는 주제가 있으면 먼저 옮기거나 접는다

지켜야 할 것:
- 흔적에 실제로 있는 것만 쓴다. 일반 상식이나 추측으로 채우지 않는다.
- 근거는 아래 보여준 흔적 번호로만 가리킨다. 번호를 지어내지 않는다.
- 이미 있는 주제(잠잠한 것 포함)와 같은 것이면 새로 만들지 말고 attach 한다.
- 나무 모양(rename, move, merge, retire)은 이유가 분명할 때만 바꾸고 reason 을 쓴다.
  바꿀 이유가 없으면 그대로 둔다 — 매일 조금씩 고치면 나무를 믿을 수 없다.
- 새 주제를 있는 주제 아래 두려면 parent 에 그 번호를 쓴다.
  같은 응답에서 만든 주제는 ref 로 가리킨다.
- 흔적 대부분은 아무 주제에도 안 붙는 게 정상이다. 억지로 붙이지 않는다.

JSON 으로만 답한다. 다른 말은 쓰지 않는다.

{"ops": [
  {"op": "create", "ref": "새1", "name": "취업", "parent": null, "evidence": [101, 102],
   "reason": "..."},
  {"op": "create", "ref": "새2", "name": "자소서", "parent": "새1", "evidence": [103, 104]},
  {"op": "attach", "topic": 3, "evidence": [105, 106]},
  {"op": "note", "topic": 3, "text": "..."},
  {"op": "move", "topic": 5, "parent": 3, "reason": "..."}
]}

바꿀 것이 없으면 {"ops": []} 만 출력한다."""


class ReflectionFailed(Exception):
    """회고가 나무에 아무것도 못 했다(응답을 못 읽음). 기록은 남겼고, 다시 돌려 볼 일이다."""


@dataclass
class Reflection:
    """한 번의 회고 결과. 로그가 아니라 **되돌아볼 수 있는 기록**이다."""

    # 프롬프트에 낱줄로 넣은 흔적 수(아직 주제에 안 붙은 것).
    considered: int = 0
    # 낱줄 대신 넣은 하루 요약 줄 수(앱 사용처럼 낱줄로 안 훑는 종류).
    digested: int = 0
    # 적용한 변경, 사람이 읽는 한 줄씩.
    applied: List[str] = field(default_factory=list)
    notes: int = 0
    rejected: List[str] = field(default_factory=list)
    notices: List[str] = field(default_factory=list)
    # 오래 근거가 없어 코드가 접은 주제.
    retired: List[str] = field(default_factory=list)


@dataclass
class Reflector:
    reasoner: Reasoner
    traces: SQLiteTraceStore
    topics: SQLiteTopicStore
    kinds: Sequence[TraceKind]
    # 한 번에 훑는 기간. 매일 돌지만 하루치만 보면 하루 한 번씩 사흘 나온 관심사를 못 알아본다 —
    # 주제에 안 붙은 흔적은 일주일 동안 다시 보여준다.
    lookback: timedelta = timedelta(days=7)
    # 종류**별** 흔적 상한이다 — 프롬프트 전체가 아니다. vLLM 은 프롬프트와 응답 상한
    # (8,192)의 합이 모델 한도 65,536 을 넘으면 거절하므로 프롬프트 몫은 약 5.7만 토큰이다.
    # 2026-10-30 실측: 웹 기록 일주일 947줄이 32,455 토큰(줄당 약 34). 지금 회고 종류는 웹 하나라
    # 1,000줄(약 3.4만 + 번호)이 들어간다. 회고 종류가 늘면 합이 넘으니 다시 잰다 — 넘으면 회고가
    # 400 으로 죽는다(조용히 잘리지는 않는다). 하루 요약에는 이 상한을 걸지 않는다.
    per_kind_limit: int = 1000
    # 주제마다 보여줄 근거 예와 메모 수.
    samples: int = 3
    notes_per_topic: int = 2

    async def run_once(self, now: datetime) -> Reflection:
        result = Reflection()
        ran_at = datetime.now(timezone.utc)
        try:
            return await self._run(now, result, ran_at)
        except Exception as exc:
            self.topics.record_run(
                now, ran_at, result.considered, result.digested, 0, 0, f"failed: {exc}"[:200]
            )
            raise

    async def _run(self, now: datetime, result: Reflection, ran_at: datetime) -> Reflection:
        # 먼저 접는다. 오래 조용한 주제를 그대로 보여주면 모델이 거기에 아무거나 붙여 되살린다.
        tree = self.topics.load()
        quiet = stale(tree, self.topics.grounds(), now)
        if quiet:
            folded = plan(tree, quiet, set(), now)
            ids = self.topics.apply(folded, now)
            result.retired = [describe(c, folded.after, ids) for c in folded.changes]

        tree = self.topics.load()
        grounds = self.topics.grounds()
        live = {n.id for n in tree.live()}
        attached = {g.trace for g in grounds if g.topic in live}

        harvest = self._gather(now, attached)
        digests = self._digest(now)
        result.considered = sum(len(v) for v in harvest.values())
        result.digested = sum(len(v) for v in digests.values())
        if not harvest and not digests:
            logger.info("회고 — 훑을 흔적이 없다")
            self.topics.record_run(now, ran_at, 0, 0, len(result.retired), 0, "empty")
            return result

        board = standings(tree, grounds, now)
        notes = self.topics.latest_notes(self.notes_per_topic)
        prompt, offered = self._build_prompt(board, notes, harvest, digests, now)
        reply = await self.reasoner.ask(prompt, system=SYSTEM_PROMPT)

        ops = _parse(reply)
        if ops is None:
            raise ReflectionFailed("회고 응답을 못 읽었다 — 나무는 그대로다")

        planned = plan(tree, ops, offered, now)
        ids = self.topics.apply(planned, now)
        result.applied = [describe(c, planned.after, ids) for c in planned.changes]
        result.notes = len(planned.notes)
        result.rejected = planned.rejected
        result.notices = planned.notices
        self.topics.record_run(
            now,
            ran_at,
            result.considered,
            result.digested,
            len(result.applied) + result.notes,
            len(result.rejected),
            "ok",
        )
        logger.info(
            "회고 — 흔적 %d줄, 하루 요약 %d줄, 변경 %d건, 메모 %d건, 기각 %d건, 접음 %d건",
            result.considered,
            result.digested,
            len(result.applied),
            result.notes,
            len(result.rejected),
            len(result.retired),
        )
        for reason in result.rejected:
            logger.info("회고 기각 — %s", reason)
        return result

    def _gather(self, now: datetime, attached: Set[int]) -> Dict[TraceKind, List[Trace]]:
        """낱줄로 훑는 종류의, 아직 아무 주제에도 안 붙은 흔적. 상한은 최신 것부터 채운다."""
        since = now - self.lookback
        found: Dict[TraceKind, List[Trace]] = {}
        for card in self.kinds:
            if not card.active or not card.reflect:
                continue
            loose = [
                t for t in self.traces.recent(card.kind, since, until=now) if t.id not in attached
            ]
            if loose:
                found[card] = loose[-self.per_kind_limit :]
        return found

    def _digest(self, now: datetime) -> Dict[TraceKind, List[str]]:
        """낱줄로 안 훑는 종류는 하루 요약으로 넣는다(요약 방법은 카드가 들고 온다).

        요약은 창 안의 흔적을 **전부** 봐야 맞다. 상한으로 자르면 앞쪽 날의 사용 시간이 아무
        표시 없이 줄어든다.
        """
        since = now - self.lookback
        found: Dict[TraceKind, List[str]] = {}
        for card in self.kinds:
            if not card.active or card.reflect or card.digest is None:
                continue
            lines = card.digest(self.traces.recent(card.kind, since, until=now), since, now)
            if lines:
                found[card] = lines
        return found

    def _build_prompt(
        self,
        board: Sequence[Standing],
        notes: Dict[Optional[int], List[Note]],
        harvest: Dict[TraceKind, List[Trace]],
        digests: Dict[TraceKind, List[str]],
        now: datetime,
    ) -> Tuple[str, Set[int]]:
        """프롬프트와, 거기 보여준 흔적 번호. 근거는 보여준 번호로만 받는다."""
        offered: Set[int] = set()
        parts: List[str] = []

        awake = [s for s in board if s.trend is not Trend.QUIET]
        if awake:
            lines: List[str] = []
            for s in awake:
                pad = "  " * s.depth
                last = f" · 마지막 {local(s.last):%m-%d}" if s.last else ""
                lines.append(
                    f"{pad}#{s.node.id} {s.node.name} — {s.trend.value} · "
                    f"근거 {s.count}개({s.days}일){last}"
                )
                for note in notes.get(s.node.id, []):
                    lines.append(f"{pad}   메모 {local(note.at):%m-%d}: {note.text}")
                for g in s.own[: self.samples]:
                    lines.append(
                        f"{pad}   근거 #{g.trace} {local(g.at):%m-%d %H:%M} {_short(g.text)}"
                    )
                    offered.add(g.trace)
            parts.append(f"[주제 나무] {len(awake)}개\n" + "\n".join(lines))
        else:
            parts.append("[주제 나무]\n  (아직 없음)")

        asleep = [s for s in board if s.trend is Trend.QUIET]
        if asleep:
            lines = [
                f"  #{s.node.id} {' › '.join(s.path)}"
                + (f" (마지막 {local(s.last):%m-%d})" if s.last else "")
                for s in asleep
            ]
            parts.append(
                "[잠잠한 주제] 4주 넘게 근거가 없다. 다시 보이면 새로 만들지 말고 여기에 붙인다\n"
                + "\n".join(lines)
            )

        loose_notes = notes.get(None, [])
        if loose_notes:
            lines = [f"  {local(n.at):%m-%d}: {n.text}" for n in loose_notes]
            parts.append("[나무 밖 메모]\n" + "\n".join(lines))

        for card, traces in harvest.items():
            lines = [f"  #{t.id} {local(t.at):%m-%d %H:%M}  {t.text}" for t in traces]
            offered.update(t.id for t in traces if t.id is not None)
            parts.append(
                f"[{card.label} — 아직 주제에 안 붙은 것] {len(traces)}건\n" + "\n".join(lines)
            )
        for card, days in digests.items():
            lines = [f"  {day}" for day in days]
            parts.append(f"[{card.label} — 하루 요약] {len(days)}일\n" + "\n".join(lines))

        parts.append(f"지금: {shown(now)}. 나무에서 바꿀 것을 연산으로 내라.")
        return "\n\n".join(parts), offered


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse(reply: str) -> Optional[List[Op]]:
    """모델 응답에서 연산 목록을 뽑는다. 못 읽으면 None — "바꿀 것 없음"(빈 목록)과 다르다.

    코드펜스로 감싸거나 앞에 설명을 붙이는 모델이 흔하다. 그때마다 회고가
    통째로 실패하는 대신 JSON 만 도려낸다 — 완벽한 형식을 요구하는 것보다
    관대하게 읽는 쪽이 실제로 잘 돈다. `{"ops": [...]}` 대신 맨 배열도 받는다.
    """
    text = reply.strip()
    if not text:
        logger.warning("회고 응답이 비었다")
        return None
    data = _json(text)
    if data is None:
        logger.warning("회고 응답에서 JSON 을 못 찾았다: %.200s", text)
        return None
    raw = data.get("ops") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        logger.warning("회고 응답에 연산 목록이 없다: %.200s", text)
        return None
    return [_op(item) for item in raw if isinstance(item, dict)]


def _json(text: str) -> Any:
    """먼저 열린 괄호부터 그 짝의 마지막 닫는 괄호까지. 맨 배열 안의 객체 하나만 집지 않게."""
    for start in sorted(i for i in (text.find("{"), text.find("[")) if i >= 0):
        end = text.rfind("}" if text[start] == "{" else "]")
        if end <= start:
            continue
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            continue
    return None


def _op(item: Dict[str, Any]) -> Op:
    evidence = item.get("evidence")
    return Op(
        op=str(item.get("op", "")).strip().lower(),
        topic=_ref(item.get("topic")),
        parent=_ref(item.get("parent")),
        into=_ref(item.get("into")),
        name=str(item.get("name") or ""),
        evidence=tuple(
            i
            for i in map(_ref, evidence if isinstance(evidence, list) else [])
            if isinstance(i, int)
        ),
        text=str(item.get("text") or ""),
        reason=str(item.get("reason") or ""),
        ref=str(item.get("ref") or "").strip(),
    )


def _ref(value: Any) -> Optional[Ref]:
    """주제·흔적 번호. "#12", "12" 도 번호로 읽는다. 번호가 아닌 글자는 ref 다."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().lstrip("#")
    if not text or text.lower() == "null":
        return None
    return int(text) if text.isdigit() else text
