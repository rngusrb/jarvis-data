"""주제 나무 — 자비스가 사용자에 대해 **추론해낸** 것.

옛 믿음(`beliefs`)을 갈아끼웠다. 믿음은 "kind 한 줄 + 문장 하나 + 근거 열둘"이라 시간 모양이
사라졌다 — 지원서 작성 23건·6일과 주거 2건·한 순간이 똑같이 한 줄이었다(2026-10-15 구조 재검토).
값(`value`)은 회고마다 통째로 새 문장으로 바뀌어 옛 근거와 새 값이 섞였고, 확신도는 LLM 이 적은
숫자라 내려가지 않았다.

나무는 LLM 이 가꾸고 코드가 지킨다.

  LLM   연산을 낸다 — 만들기, 붙이기, 메모, 이름 바꾸기, 옮기기, 합치기, 접기
  코드  연산을 나무 사본 위에서 하나씩 검사하고(`plan`), 통과한 것만 저장소가 한꺼번에 적용한다

데이터 쪽 원칙(BACKLOG "정한 것"):
  - 이름이 아니라 번호로 잇는다
  - 근거는 옮기지 않고 연결만 바꾼다. 옛 연결도 지우지 않는다(뗀 때를 적는다)
  - 모든 구조 변경을 이유와 함께 기록한다
  - 한 회고의 변경은 한 트랜잭션에 적용한다

하이라이트(뜨는 중·꾸준·시드는 중)는 저장하지 않는다. 근거의 시간 분포에서 볼 때마다 계산한다 —
옛 `status` 칸처럼 저장해 두면 며칠 꺼져 있던 동안 시들어야 할 것이 안 시든다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from src.core.clock import local

DAY = timedelta(days=1)
# 하이라이트는 최근 7일과 그 앞 21일의 "근거가 있던 날" 비율을 견준다. 날을 세는 건 한 번에
# 쏟아진 검색 쉰 개가 꾸준한 관심으로 읽히지 않게 하려는 것이다.
RECENT_DAYS = 7
BEFORE_DAYS = 21
# 이만큼 근거가 없으면 코드가 접는다. 줄이는 쪽이 없으면 몇 달 뒤 옛 관심사가 프롬프트를 채운다.
RETIRE_AFTER = timedelta(days=60)
# 새 주제의 최소 근거. 흔적 하나짜리는 관심사가 아니라 스쳐간 호기심이다(옛 회고 규칙).
MIN_EVIDENCE = 2
MAX_NAME = 40
# 나무 모양을 바꾸는 연산. 모델이 이걸 매일 내면 나무를 믿을 수 없다(3단계 끝 기준).
RESHAPING = ("rename", "move", "merge", "retire")
# 누가 바꿨나. 오래 잠잠해 코드가 접은 것을 모델의 구조 변경으로 세면 변경 빈도가 부풀려진다.
MODEL = "model"
CODE = "code"

Ref = Union[int, str]


class Trend(str, Enum):
    NEW = "처음 보임"  # 근거가 하루뿐 — 옛 "후보". 자비스의 맥락에는 안 넣는다
    RISING = "뜨는 중"
    STEADY = "꾸준"
    FADING = "시드는 중"
    QUIET = "잠잠"  # 최근 28일(RECENT_DAYS + BEFORE_DAYS) 근거 없음


@dataclass
class Node:
    id: int
    name: str
    parent: Optional[int]
    created_at: datetime
    retired_at: Optional[datetime] = None

    @property
    def live(self) -> bool:
        return self.retired_at is None


@dataclass
class Tree:
    """저장소에서 읽은 나무 한 벌. 검사는 이걸 복사해 그 위에서 한다."""

    nodes: Dict[int, Node] = field(default_factory=dict)
    # 주제 → 지금 붙어 있는 흔적 번호. 뗀 연결은 여기 없다(저장소에는 남는다).
    links: Dict[int, Set[int]] = field(default_factory=dict)
    # 읽어 온 때의 변경 기록 끝 번호. 적용할 때 그새 누가 나무를 바꿨는지 본다.
    version: int = 0

    def copy(self) -> "Tree":
        return Tree(
            nodes={k: replace(v) for k, v in self.nodes.items()},
            links={k: set(v) for k, v in self.links.items()},
            version=self.version,
        )

    def live(self) -> List[Node]:
        return sorted((n for n in self.nodes.values() if n.live), key=lambda n: n.id)

    def children(self, topic: Optional[int]) -> List[Node]:
        return [n for n in self.live() if n.parent == topic]

    def ancestors(self, topic: int) -> List[int]:
        found: List[int] = []
        parent = self.nodes[topic].parent
        while parent is not None and parent not in found:
            found.append(parent)
            parent = self.nodes[parent].parent
        return found

    def subtree(self, topic: int) -> Set[int]:
        """살아 있는 자손과 자기 자신."""
        found = {topic}
        frontier = [topic]
        while frontier:
            kids = [n.id for n in self.children(frontier.pop()) if n.id not in found]
            found.update(kids)
            frontier.extend(kids)
        return found

    def lineage(self, topic: int) -> Set[int]:
        """자기와 **모든** 자손 — 접힌 가지까지.

        근거를 셀 때 쓴다. 가지를 접어도(마감이 끝난 자소서) 그 근거는 줄기(취업)의 이력이다.
        살아 있는 자손만 세면 가지를 접는 순간 줄기의 근거가 0 이 되고, 다음 회고가 "60일 동안
        근거가 없다"는 거짓 이유로 줄기를 접는다(JAR-021 리뷰 재현). 합쳐진 주제는 연결을 다 넘겨서
        보태는 게 없다.
        """
        found = {topic}
        frontier = [topic]
        while frontier:
            current = frontier.pop()
            kids = [n.id for n in self.nodes.values() if n.parent == current and n.id not in found]
            found.update(kids)
            frontier.extend(kids)
        return found

    def path(self, topic: int) -> Tuple[str, ...]:
        names = [self.nodes[a].name for a in reversed(self.ancestors(topic))]
        return (*names, self.nodes[topic].name)

    def sibling_named(
        self, parent: Optional[int], name: str, exclude: Optional[int] = None
    ) -> Optional[Node]:
        for node in self.children(parent):
            if node.id != exclude and _same_name(node.name, name):
                return node
        return None


@dataclass(frozen=True)
class Ground:
    """주제에 붙은 근거 하나 — 흔적 번호와, 그 흔적이 일어난 때와 글."""

    topic: int
    trace: int
    at: datetime
    text: str


@dataclass(frozen=True)
class Op:
    """LLM 이 낸 연산 하나. 번호(int)는 있는 주제, 글자(str)는 같은 응답에서 만든 주제의 ref 다."""

    op: str
    topic: Optional[Ref] = None
    parent: Optional[Ref] = None
    into: Optional[Ref] = None
    name: str = ""
    evidence: Tuple[int, ...] = ()
    text: str = ""
    reason: str = ""
    ref: str = ""

    def label(self) -> str:
        target = self.topic if self.topic is not None else self.name
        return f"{self.op} {target}".strip()


@dataclass(frozen=True)
class Change:
    """적용할 변경 한 줄. `topic` 이 음수면 이번에 만든 주제다 — 저장소가 진짜 번호로 바꾼다."""

    op: str
    topic: int
    detail: Dict[str, Any]
    reason: str = ""
    actor: str = MODEL


@dataclass(frozen=True)
class ChangeRecord:
    """저장소에서 다시 읽은 변경 한 줄."""

    at: datetime
    op: str
    topic: Optional[int]
    detail: Dict[str, Any]
    reason: str
    actor: str


@dataclass(frozen=True)
class NoteDraft:
    topic: Optional[int]
    text: str


@dataclass
class Plan:
    before: Tree
    after: Tree
    changes: List[Change] = field(default_factory=list)
    notes: List[NoteDraft] = field(default_factory=list)
    # 통째로 뺀 연산과 그 이유.
    rejected: List[str] = field(default_factory=list)
    # 받되 고친 것(보여주지 않은 흔적 번호를 뺌, 같은 이름이라 새로 안 만들고 붙임).
    notices: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.changes and not self.notes


class Rejected(Exception):
    pass


def plan(
    tree: Tree, ops: Sequence[Op], offered: Set[int], now: datetime, actor: str = MODEL
) -> Plan:
    """연산을 나무 사본 위에서 차례로 검사한다. 원본 나무는 건드리지 않는다.

    `offered` 는 이번 프롬프트에 **보여준** 흔적 번호다. 근거는 이 안에서만 받는다 — 모델이
    지어낸 번호나 보지도 않은 흔적이 근거가 되면 출처 추적이라는 이 구조의 존재 이유가 깨진다.

    틀린 연산은 이유와 함께 빼고 다음 연산으로 간다. 하나가 틀렸다고 하루치 회고를 버리면 매일
    거의 아무것도 안 남는다. 뒤 연산이 뺀 create 의 ref 를 가리키면 그것도 이유와 함께 빠진다.
    """
    planner = _Planner(tree.copy(), offered, now, actor)
    for op in ops:
        try:
            planner.apply(op)
        except Rejected as exc:
            planner.result.rejected.append(f"{op.label()}: {exc}")
    planner.result.before = tree
    return planner.result


def stale(tree: Tree, grounds: Sequence[Ground], now: datetime) -> List[Op]:
    """오래 근거가 없는 주제를 접는 연산(`plan(..., actor=CODE)` 로 검사한다).

    아래부터 접는다 — 살아 있는 가지가 있으면 접을 수 없다. 근거는 접힌 가지까지 센다(`lineage`).
    근거가 하나도 안 남은 주제(가지가 옮겨 가거나 합쳐져 빈 줄기)는 바로 접는다 — 만든 때를 마지막
    근거로 치면 "60일 동안 근거가 없다"는 거짓 이유가 남는다.
    """
    last = _last_seen(tree, grounds)
    doomed: List[Tuple[Node, str]] = []
    for node in tree.live():
        seen = last.get(node.id)
        if seen is None:
            doomed.append((node, "근거가 하나도 남지 않았다(가지가 옮겨 가거나 합쳐졌다)"))
        elif now - seen >= RETIRE_AFTER:
            days = int((now - seen) / DAY)
            doomed.append((node, f"{days}일 동안 근거가 없다(마지막 {local(seen):%m-%d})"))
    doomed.sort(key=lambda d: len(tree.ancestors(d[0].id)), reverse=True)
    return [Op("retire", topic=node.id, reason=reason) for node, reason in doomed]


def trend(times: Sequence[datetime], now: datetime) -> Trend:
    """근거가 있던 날의 분포로 본 주제의 지금. 날은 `now` 에서 24시간씩 거꾸로 센다."""
    ages = {int((now - t) / DAY) for t in times if t <= now}
    if not ages:
        return Trend.QUIET
    recent = sum(1 for a in ages if a < RECENT_DAYS)
    before = sum(1 for a in ages if RECENT_DAYS <= a < RECENT_DAYS + BEFORE_DAYS)
    if recent == 0:
        return Trend.FADING if before else Trend.QUIET
    if len(ages) == 1:
        return Trend.NEW
    # 날 비율(최근 7일 대 그 앞 21일)이 두 배 이상이면 뜨고, 절반 이하면 시든다.
    if recent * BEFORE_DAYS >= 2 * before * RECENT_DAYS:
        return Trend.RISING
    if 2 * recent * BEFORE_DAYS <= before * RECENT_DAYS:
        return Trend.FADING
    return Trend.STEADY


@dataclass(frozen=True)
class Standing:
    """주제 하나의 지금 모습. 가지의 근거까지 합쳐 센다 — 접힌 가지도, 같은 흔적은 한 번."""

    node: Node
    depth: int
    path: Tuple[str, ...]
    trend: Trend
    count: int
    days: int
    recent_days: int
    last: Optional[datetime]
    # 이 주제에 **직접** 붙은 근거, 최근 것부터.
    own: Tuple[Ground, ...]


def standings(tree: Tree, grounds: Sequence[Ground], now: datetime) -> List[Standing]:
    """살아 있는 주제를 줄기부터 가지 순서로(번호순).

    순서가 매일 같아야 모델이 나무를 다시 짜지 않는다.
    """
    own: Dict[int, List[Ground]] = {}
    for g in grounds:
        own.setdefault(g.topic, []).append(g)

    found: List[Standing] = []

    def visit(node: Node, depth: int) -> None:
        merged: Dict[int, Ground] = {}
        for topic in tree.lineage(node.id):
            for g in own.get(topic, []):
                merged.setdefault(g.trace, g)
        times = [g.at for g in merged.values()]
        ages = {int((now - t) / DAY) for t in times if t <= now}
        found.append(
            Standing(
                node=node,
                depth=depth,
                path=tree.path(node.id),
                trend=trend(times, now),
                count=len(merged),
                days=len(ages),
                recent_days=sum(1 for a in ages if a < RECENT_DAYS),
                last=max(times) if times else None,
                own=tuple(sorted(own.get(node.id, []), key=lambda g: g.at, reverse=True)),
            )
        )
        for kid in tree.children(node.id):
            visit(kid, depth + 1)

    live = {n.id for n in tree.live()}
    for node in tree.live():
        # 부모가 접혔는데 살아 있는 가지는 원래 없다(접기·합치기가 막는다). 있으면 줄기로 보여준다.
        if node.parent is None or node.parent not in live:
            visit(node, 0)
    return found


def describe(change: Change, tree: Tree, ids: Mapping[int, int]) -> str:
    """적용한 변경을 사람이 읽는 한 줄로. `ids` 는 임시 번호 → 진짜 번호."""

    def name(topic: Optional[int]) -> str:
        if topic is None:
            return "큰 줄기"
        return f"#{ids.get(topic, topic)} {' › '.join(tree.path(topic))}"

    why = f" — {change.reason}" if change.reason else ""
    d = change.detail
    if change.op == "create":
        return f"새 주제 {name(change.topic)} (근거 {len(d['evidence'])}){why}"
    if change.op == "attach":
        return f"{name(change.topic)} +{len(d['evidence'])}"
    if change.op == "rename":
        return f"#{ids.get(change.topic, change.topic)} 이름 '{d['from']}' → '{d['to']}'{why}"
    if change.op == "move":
        return f"{name(change.topic)} 옮김 (전: {name(d['from'])}){why}"
    if change.op == "merge":
        return f"#{ids.get(change.topic, change.topic)} 을 {name(d['into'])} 로 합침{why}"
    return f"{name(change.topic)} 접음{why}"


@dataclass(frozen=True)
class Churn:
    """나무가 얼마나 자주 바뀌었나 — 3단계 끝 기준("나무가 너무 자주 바뀌지 않는지")의 재료."""

    days: int
    # 모델이 낸 변경, 연산별.
    ops: Dict[str, int]
    # 오래 잠잠해 코드가 접은 것. 모델의 구조 변경이 아니다.
    auto_retired: int
    # 모델이 나무 모양(이름·자리·합치기·접기)을 바꾼 날 수와, 그날들의 변경 수(많은 것부터).
    reshaped_days: int
    busiest: Tuple[int, ...]


def churn(records: Sequence[ChangeRecord], days: int) -> Churn:
    """변경 기록을 센다. 날은 한국 날짜로 — 회고는 하루 한 번(새벽) 돈다."""
    model = [r for r in records if r.actor == MODEL]
    reshaping = Counter(local(r.at).date() for r in model if r.op in RESHAPING)
    return Churn(
        days=days,
        ops=dict(Counter(r.op for r in model)),
        auto_retired=sum(1 for r in records if r.actor == CODE and r.op == "retire"),
        reshaped_days=len(reshaping),
        busiest=tuple(sorted(reshaping.values(), reverse=True)),
    )


# ── 검사 ─────────────────────────────────────────────


class _Planner:
    def __init__(self, tree: Tree, offered: Set[int], now: datetime, actor: str) -> None:
        self.tree = tree
        self.offered = offered
        self.now = now
        self.actor = actor
        self.refs: Dict[str, int] = {}
        # 빠진 create 의 ref. 뒤에서 가리키면 같은 번호의 있는 주제로 새지 않고 같이 빠진다.
        self.dead: Set[str] = set()
        self.next_id = -1
        self.result = Plan(before=tree, after=tree)

    def apply(self, op: Op) -> None:
        handler = {
            "create": self._create,
            "attach": self._attach,
            "note": self._note,
            "rename": self._rename,
            "move": self._move,
            "merge": self._merge,
            "retire": self._retire,
        }.get(op.op)
        if handler is None:
            raise Rejected(f"모르는 연산 '{op.op}'")
        try:
            handler(op)
        except Rejected:
            if op.op == "create" and op.ref:
                self.dead.add(op.ref)
            raise

    def _change(self, op: str, topic: int, detail: Dict[str, Any], reason: str) -> None:
        self.result.changes.append(Change(op, topic, detail, reason.strip(), self.actor))

    # 각 연산은 검사를 다 마친 뒤에만 나무를 바꾼다 — 빠지는 연산이 반쯤 적용되면 안 된다.

    def _create(self, op: Op) -> None:
        name = _name(op.name)
        parent = self._resolve(op.parent) if op.parent is not None else None
        if op.ref and (op.ref in self.refs or op.ref in self.dead):
            raise Rejected(f"ref '{op.ref}' 를 두 번 썼다")
        evidence, unseen = self._evidence(op)
        same = self.tree.sibling_named(parent, name)
        if same is not None:
            # 옛 사고: 모델이 한 응답에서 같은 kind 를 네 번 내 값과 근거의 짝이 어긋났다.
            # 같은 자리에 같은 이름이면 새로 만들지 않고 거기 붙인다.
            self._name_ref(op, same.id)
            added = self._link_all(same.id, evidence, op.reason)
            what = f"+{added}" if added else "근거도 이미 붙어 있다"
            self._notice(op, f"#{same.id} 와 이름이 같아 거기 붙였다({what})", unseen)
            return
        if len(evidence) < MIN_EVIDENCE:
            raise Rejected(
                f"근거가 {len(evidence)}개뿐 — 새 주제는 서로 다른 흔적 {MIN_EVIDENCE}개부터"
                + _unseen(unseen)
            )

        topic = self.next_id
        self.next_id -= 1
        self.tree.nodes[topic] = Node(topic, name, parent, self.now)
        self.tree.links[topic] = set()
        self._name_ref(op, topic)
        for trace in evidence:
            self._link(topic, trace)
        self._change(
            "create", topic, {"name": name, "parent": parent, "evidence": evidence}, op.reason
        )
        self._notice(op, "", unseen)

    def _attach(self, op: Op) -> None:
        topic = self._resolve(op.topic)
        evidence, unseen = self._evidence(op)
        if not evidence:
            raise Rejected("붙일 흔적이 없다" + _unseen(unseen))
        self._link_all(topic, evidence, op.reason)
        self._notice(op, "", unseen)

    def _note(self, op: Op) -> None:
        topic = self._resolve(op.topic) if op.topic is not None else None
        text = op.text.strip()
        if not text:
            raise Rejected("빈 메모")
        self.result.notes.append(NoteDraft(topic, text))

    def _rename(self, op: Op) -> None:
        topic = self._resolve(op.topic)
        name = _name(op.name)
        reason = _reason(op)
        node = self.tree.nodes[topic]
        if name == node.name:
            return
        clash = self.tree.sibling_named(node.parent, name, exclude=topic)
        if clash is not None:
            raise Rejected(
                f"같은 자리의 #{clash.id} 가 이미 '{clash.name}' 이다 — 같은 것이면 merge"
            )
        before = node.name
        node.name = name
        self._change("rename", topic, {"from": before, "to": name}, reason)

    def _move(self, op: Op) -> None:
        topic = self._resolve(op.topic)
        parent = self._resolve(op.parent) if op.parent is not None else None
        reason = _reason(op)
        node = self.tree.nodes[topic]
        if parent == node.parent:
            return
        if parent is not None and parent in self.tree.subtree(topic):
            raise Rejected("자기 아래로는 옮길 수 없다")
        clash = self.tree.sibling_named(parent, node.name, exclude=topic)
        if clash is not None:
            raise Rejected(f"옮길 자리에 같은 이름(#{clash.id})이 있다 — 같은 것이면 merge")
        before = node.parent
        node.parent = parent
        self._settle(topic)
        self._change("move", topic, {"from": before, "to": parent}, reason)

    def _merge(self, op: Op) -> None:
        topic = self._resolve(op.topic)
        into = self._resolve(op.into)
        reason = _reason(op)
        if topic == into:
            raise Rejected("자기 자신과는 합칠 수 없다")
        if into in self.tree.subtree(topic):
            raise Rejected("자기 아래 주제로는 합칠 수 없다")
        kids = self.tree.children(topic)
        for kid in kids:
            # 가지를 부모로 합칠 때 그 가지 자신은 곧 접힌다 — 이름이 같아도 겹치는 게 아니다.
            clash = self.tree.sibling_named(into, kid.name, exclude=topic)
            if clash is not None:
                raise Rejected(
                    f"가지 #{kid.id} 가 #{into} 아래 #{clash.id} 와 이름이 겹친다"
                    " — 그 둘부터 합친다"
                )

        # 가지부터 옮긴다. 근거를 붙일 때 더 구체적인 자리(옮겨 온 가지)에 이미 있는지 봐야 한다.
        for kid in kids:
            kid.parent = into
        moved: List[int] = []
        for trace in sorted(self.tree.links.get(topic, set())):
            self.tree.links[topic].discard(trace)
            if self._link(into, trace):
                moved.append(trace)
        self.tree.nodes[topic].retired_at = self.now
        self._settle(into)
        detail = {"into": into, "evidence": moved, "children": [k.id for k in kids]}
        self._change("merge", topic, detail, reason)

    def _retire(self, op: Op) -> None:
        topic = self._resolve(op.topic)
        reason = _reason(op)
        kids = self.tree.children(topic)
        if kids:
            raise Rejected(f"아래에 살아 있는 주제가 {len(kids)}개 있다 — 먼저 옮기거나 접는다")
        # 근거 연결은 그대로 둔다. 접힌 주제도 왜 있었는지는 남아야 하고, 줄기의 근거로도 남는다.
        self.tree.nodes[topic].retired_at = self.now
        self._change("retire", topic, {}, reason)

    # ── 도구 ──

    def _resolve(self, ref: Optional[Ref]) -> int:
        """같은 응답에서 지은 ref 가 먼저다 — ref 를 숫자("1")로 지으면 그 뒤의 1 은 새 주제다.

        있는 번호를 먼저 보면 "1" 로 지은 새 주제에 줄 메모가 아무 표시 없이 있는 #1 로 간다
        (JAR-021 리뷰 재현). 빠진 create 의 ref 를 가리키면 같이 뺀다.
        """
        if ref is None:
            raise Rejected("주제 번호가 없다")
        key = str(ref)
        if key in self.refs:
            topic = self.refs[key]
        elif key in self.dead:
            raise Rejected(f"앞에서 빠진 create(ref '{key}')를 가리킨다")
        elif isinstance(ref, str):
            raise Rejected(f"모르는 ref '{ref}'")
        else:
            topic = ref
        node = self.tree.nodes.get(topic)
        if node is None:
            raise Rejected(f"#{topic} 은 없는 주제다")
        if not node.live:
            raise Rejected(f"#{topic} 은 접힌 주제다")
        return topic

    def _evidence(self, op: Op) -> Tuple[List[int], int]:
        """보여준 번호만 고른다. 보여주지 않은 번호 수도 돌려준다 — 지어낸 근거의 흔적이다."""
        picked: List[int] = []
        unseen = 0
        for trace in op.evidence:
            if trace not in self.offered:
                unseen += 1
            elif trace not in picked:
                picked.append(trace)
        return picked, unseen

    def _notice(self, op: Op, what: str, unseen: int) -> None:
        """받은 연산에 고친 게 있으면 남긴다. 빠진 연산의 사정은 기각 이유에 들어간다."""
        parts = [p for p in (what, _unseen(unseen).strip(" ()")) if p]
        if parts:
            self.result.notices.append(f"{op.label()}: {', '.join(parts)}")

    def _name_ref(self, op: Op, topic: int) -> None:
        if not op.ref:
            return
        if op.ref.isdigit() and int(op.ref) in self.tree.nodes:
            self.result.notices.append(
                f"{op.label()}: ref '{op.ref}' 가 있는 주제 #{op.ref} 와 겹친다"
                f" — 이 응답에서 {op.ref} 는 새 주제다"
            )
        self.refs[op.ref] = topic

    def _link_all(self, topic: int, evidence: Sequence[int], reason: str) -> int:
        added = [trace for trace in evidence if self._link(topic, trace)]
        if added:
            self._change("attach", topic, {"evidence": added}, reason)
        return len(added)

    def _settle(self, topic: int) -> None:
        """옮기거나 합친 뒤 "가장 구체적인 주제에 붙는다"를 다시 세운다.

        새 조상에 같은 흔적이 남아 있으면 닫는다. 안 닫으면 같은 근거가 줄기와 가지에 함께 열려
        프롬프트의 근거 예에 겹쳐 나온다(리뷰 퍼즈에서 최대 11건). 근거 수는 어차피 한 번만 센다.
        """
        for node in self.tree.subtree(topic):
            held = self.tree.links.get(node, set())
            for ancestor in self.tree.ancestors(node):
                self.tree.links.get(ancestor, set()).difference_update(held)

    def _link(self, topic: int, trace: int) -> bool:
        """가장 구체적인 주제에 붙인다. 가지에 붙이면 조상의 같은 연결은 닫는다.

        가지(자소서)와 줄기(취업)에 같은 흔적이 둘 다 붙어 있으면 줄기에서 "아직 가지로 안 나뉜
        근거"가 안 보인다. 줄기의 근거 수는 어차피 가지까지 합쳐 센다(`standings`).
        """
        if any(trace in self.tree.links.get(t, set()) for t in self.tree.subtree(topic)):
            return False
        for ancestor in self.tree.ancestors(topic):
            self.tree.links.get(ancestor, set()).discard(trace)
        self.tree.links.setdefault(topic, set()).add(trace)
        return True


def _name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw).strip()
    if not name:
        raise Rejected("이름이 없다")
    if len(name) > MAX_NAME:
        raise Rejected(f"이름이 {len(name)}자다 — {MAX_NAME}자까지")
    return name


def _reason(op: Op) -> str:
    reason = op.reason.strip()
    if not reason:
        raise Rejected("이유가 없다 — 나무 모양을 바꿀 땐 이유를 적는다")
    return reason


def _same_name(a: str, b: str) -> bool:
    return re.sub(r"\s+", "", a).lower() == re.sub(r"\s+", "", b).lower()


def _unseen(count: int) -> str:
    return f" (보여주지 않은 흔적 번호 {count}개는 뺐다)" if count else ""


def _last_seen(tree: Tree, grounds: Sequence[Ground]) -> Dict[int, datetime]:
    own: Dict[int, datetime] = {}
    for g in grounds:
        if g.topic not in own or g.at > own[g.topic]:
            own[g.topic] = g.at
    last: Dict[int, datetime] = {}
    for node in tree.live():
        seen = [own[t] for t in tree.lineage(node.id) if t in own]
        if seen:
            last[node.id] = max(seen)
    return last
