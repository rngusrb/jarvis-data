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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

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


def plan(tree: Tree, ops: Sequence[Op], offered: Set[int], now: datetime) -> Plan:
    """연산을 나무 사본 위에서 차례로 검사한다. 원본 나무는 건드리지 않는다.

    `offered` 는 이번 프롬프트에 **보여준** 흔적 번호다. 근거는 이 안에서만 받는다 — 모델이
    지어낸 번호나 보지도 않은 흔적이 근거가 되면 출처 추적이라는 이 구조의 존재 이유가 깨진다.

    틀린 연산은 이유와 함께 빼고 다음 연산으로 간다. 하나가 틀렸다고 하루치 회고를 버리면 매일
    거의 아무것도 안 남는다. 뒤 연산이 뺀 연산의 ref 를 가리키면 그것도 "모르는 ref" 로 빠진다.
    """
    planner = _Planner(tree.copy(), offered, now)
    for op in ops:
        try:
            planner.apply(op)
        except Rejected as exc:
            planner.result.rejected.append(f"{op.label()}: {exc}")
    planner.result.before = tree
    return planner.result


def stale(tree: Tree, grounds: Sequence[Ground], now: datetime) -> List[Op]:
    """오래 근거가 없는 주제를 접는 연산. 아래부터 접는다 — 살아 있는 가지가 있으면 접을 수 없다."""
    last = _last_seen(tree, grounds)
    doomed = [n for n in tree.live() if now - last.get(n.id, n.created_at) >= RETIRE_AFTER]
    doomed.sort(key=lambda n: len(tree.ancestors(n.id)), reverse=True)
    reason = f"{RETIRE_AFTER.days}일 동안 근거가 없다"
    return [Op("retire", topic=n.id, reason=reason) for n in doomed]


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
    """주제 하나의 지금 모습. 가지의 근거까지 합쳐 센다(같은 흔적은 한 번)."""

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
        for topic in tree.subtree(node.id):
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


# ── 검사 ─────────────────────────────────────────────


class _Planner:
    def __init__(self, tree: Tree, offered: Set[int], now: datetime) -> None:
        self.tree = tree
        self.offered = offered
        self.now = now
        self.refs: Dict[str, int] = {}
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
        handler(op)

    # 각 연산은 검사를 다 마친 뒤에만 나무를 바꾼다 — 빠지는 연산이 반쯤 적용되면 안 된다.

    def _create(self, op: Op) -> None:
        name = _name(op.name)
        parent = self._resolve(op.parent) if op.parent is not None else None
        if op.ref and op.ref in self.refs:
            raise Rejected(f"ref '{op.ref}' 를 두 번 썼다")
        evidence = self._evidence(op)
        same = self.tree.sibling_named(parent, name)
        if same is not None:
            # 옛 사고: 모델이 한 응답에서 같은 kind 를 네 번 내 값과 근거의 짝이 어긋났다.
            # 같은 자리에 같은 이름이면 새로 만들지 않고 거기 붙인다.
            if op.ref:
                self.refs[op.ref] = same.id
            self.result.notices.append(f"{op.label()}: #{same.id} 와 이름이 같아 거기 붙였다")
            self._link_all(same.id, evidence, op.reason)
            return
        if len(evidence) < MIN_EVIDENCE:
            raise Rejected(
                f"근거가 {len(evidence)}개뿐 — 새 주제는 서로 다른 흔적 {MIN_EVIDENCE}개부터"
            )

        topic = self.next_id
        self.next_id -= 1
        self.tree.nodes[topic] = Node(topic, name, parent, self.now)
        self.tree.links[topic] = set()
        if op.ref:
            self.refs[op.ref] = topic
        for trace in evidence:
            self._link(topic, trace)
        detail = {"name": name, "parent": parent, "evidence": evidence}
        self.result.changes.append(Change("create", topic, detail, op.reason.strip()))

    def _attach(self, op: Op) -> None:
        topic = self._resolve(op.topic)
        evidence = self._evidence(op)
        if not evidence:
            raise Rejected("붙일 흔적이 없다")
        self._link_all(topic, evidence, op.reason)

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
        self.result.changes.append(Change("rename", topic, {"from": before, "to": name}, reason))

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
        self.result.changes.append(Change("move", topic, {"from": before, "to": parent}, reason))

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
            clash = self.tree.sibling_named(into, kid.name)
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
        detail = {"into": into, "evidence": moved, "children": [k.id for k in kids]}
        self.result.changes.append(Change("merge", topic, detail, reason))

    def _retire(self, op: Op) -> None:
        topic = self._resolve(op.topic)
        reason = _reason(op)
        kids = self.tree.children(topic)
        if kids:
            raise Rejected(f"아래에 살아 있는 주제가 {len(kids)}개 있다 — 먼저 옮기거나 접는다")
        # 근거 연결은 그대로 둔다. 접힌 주제도 왜 있었는지는 남아야 한다.
        self.tree.nodes[topic].retired_at = self.now
        self.result.changes.append(Change("retire", topic, {}, reason))

    # ── 도구 ──

    def _resolve(self, ref: Optional[Ref]) -> int:
        if ref is None:
            raise Rejected("주제 번호가 없다")
        if isinstance(ref, str):
            if ref not in self.refs:
                raise Rejected(f"모르는 ref '{ref}'")
            topic = self.refs[ref]
        elif ref not in self.tree.nodes and str(ref) in self.refs:
            # 모델이 ref 를 숫자로 지었다("1"). 있는 주제 번호와 안 겹치면 ref 로 본다.
            topic = self.refs[str(ref)]
        else:
            topic = ref
        node = self.tree.nodes.get(topic)
        if node is None:
            raise Rejected(f"#{topic} 은 없는 주제다")
        if not node.live:
            raise Rejected(f"#{topic} 은 접힌 주제다")
        return topic

    def _evidence(self, op: Op) -> List[int]:
        picked: List[int] = []
        unseen = 0
        for trace in op.evidence:
            if trace not in self.offered:
                unseen += 1
            elif trace not in picked:
                picked.append(trace)
        if unseen:
            self.result.notices.append(f"{op.label()}: 보여주지 않은 흔적 번호 {unseen}개는 뺐다")
        return picked

    def _link_all(self, topic: int, evidence: Sequence[int], reason: str) -> None:
        added = [trace for trace in evidence if self._link(topic, trace)]
        if added:
            self.result.changes.append(Change("attach", topic, {"evidence": added}, reason.strip()))

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


def _last_seen(tree: Tree, grounds: Sequence[Ground]) -> Dict[int, datetime]:
    own: Dict[int, datetime] = {}
    for g in grounds:
        if g.topic not in own or g.at > own[g.topic]:
            own[g.topic] = g.at
    last: Dict[int, datetime] = {}
    for node in tree.live():
        seen = [own[t] for t in tree.subtree(node.id) if t in own]
        if seen:
            last[node.id] = max(seen)
    return last
