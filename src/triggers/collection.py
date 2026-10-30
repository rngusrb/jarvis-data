"""수집 감시 — 데이터가 안 들어오는 것 자체를 신호로 다룬다.

지표마다 감시를 하나씩 두던 때가 있었다(`stale_data:<지표>`). 폰 연결이 끊기면
수면, 심박, 위치가 각각 따로 경고했고, 자비스는 매일 아침 "자동화 설정을 확인해"를
반복했다. 실제 원인은 폰의 Tailscale이었다.

    사고 이력: 2026-10-03 ~ 10-15 텔레그램으로 열 번 경고했는데 데이터는 계속
    끊겨 있었다. 조언이 틀려서 아무 일도 일어나지 않았다.

그래서 감시를 하나로 합친다. 무엇이 끊겼는지를 기기별로 모으고, 같은 기기에서
오는 것이 비슷한 때 전부 끊겼으면 수집 경로 하나가 아니라 **그 기기와 서버 사이
연결**을 의심한다. 말은 하루 한 번, 원인과 함께 한다.

## 창이 없다

트리거는 최근 구간(창)을 받아 판단하지만, 이 감시는 "마지막으로 들어온 때"만 본다.

    사고 이력: 2026-08-29 수집이 멈췄는데 9월 13일부터 경고가 끊겼다. 창이 14일이라
    마지막 기록이 창 밖으로 밀려났고, 빈 창은 "아직 시작 안 함"으로 판정됐다.
    중단이 심해질수록 감시가 조용해졌다.

그래서 트리거가 아니라 별도의 계약(`Watch`)이다. 받는 것은 저장소가 아니라
"종류별 마지막 수신 시각" 한 장이라, 판단 자체는 여전히 순수 함수다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Mapping, Optional, Sequence

from src.core.models import Insight, Severity


@dataclass(frozen=True)
class Watched:
    """감시 대상 하나. 지표 카드나 흔적 카드에서 만든다."""

    kind: str
    label: str
    collector: str
    via: str
    stale_after: timedelta


@dataclass
class CollectionWatch:
    sources: Sequence[Watched]
    name: str = "collection"
    # 이만큼 끊기면 심각하다고 본다.
    urgent_after: timedelta = timedelta(days=3)
    # 마지막 수신 시각들이 이 안에 모여 있으면 "한꺼번에 끊겼다"고 본다. 수면처럼
    # 하루 단위로 접는 지표는 날짜의 자정이 찍혀서, 같은 날 끊겨도 위치와 하루 가까이
    # 차이가 날 수 있다.
    together_within: timedelta = timedelta(hours=36)
    # 기기 전체가 끊겼을 때 무엇을 확인하라고 할지. 배포 환경에 따라 다르므로
    # 조립하는 쪽(app/main.py)이 채운다.
    hints: Mapping[str, str] = field(default_factory=dict)

    def check(self, last_seen: Mapping[str, datetime], now: datetime) -> Optional[Insight]:
        by_via: Dict[str, List[Watched]] = {}
        for source in self.sources:
            # 한 번도 안 들어온 건 "멈춤"이 아니라 "아직 시작 안 함"이다.
            # 설정을 마치기도 전에 잔소리를 듣게 할 이유가 없다.
            if source.kind in last_seen:
                by_via.setdefault(source.via, []).append(source)

        lines: List[str] = []
        oldest = timedelta(0)
        for via, sources in sorted(by_via.items()):
            stale = [s for s in sources if now - last_seen[s.kind] >= s.stale_after]
            if not stale:
                continue
            ages = {s.kind: now - last_seen[s.kind] for s in stale}
            oldest = max(oldest, *ages.values())
            listed = ", ".join(f"{s.label}({_age(ages[s.kind])})" for s in stale)

            fine = [s for s in sources if s not in stale]
            times = [last_seen[s.kind] for s in stale]
            together = max(times) - min(times) <= self.together_within
            if not fine and len(stale) > 1 and together:
                hint = self.hints.get(via, f"{_josa(via, '과', '와')} 서버 사이 연결")
                lines.append(
                    f"- {via}: {listed}. {via}에서 오는 것이 전부 비슷한 때 끊겼다. "
                    f"수집 경로 하나가 아니라 연결 문제일 가능성이 크다. 확인할 것: {hint}."
                )
            elif fine:
                ok = _josa(", ".join(s.label for s in fine), "은", "는")
                paths = "; ".join(f"{_josa(s.label, '은', '는')} {s.collector}" for s in stale)
                lines.append(
                    f"- {via}: {listed}. 같은 {via}의 {ok} 정상이라 연결 문제는 아니다. "
                    f"확인할 것: {paths}."
                )
            else:
                paths = "; ".join(f"{_josa(s.label, '은', '는')} {s.collector}" for s in stale)
                lines.append(f"- {via}: {listed}. 끊긴 때가 서로 달라 하나씩 확인할 것: {paths}.")

        if not lines:
            return None
        severity = Severity.URGENT if oldest >= self.urgent_after else Severity.NOTABLE
        return Insight(
            trigger=self.name,
            summary="수집이 끊겼다.\n" + "\n".join(lines),
            severity=severity,
            at=now,
        )


def _age(age: timedelta) -> str:
    hours = age.total_seconds() / 3600
    return f"{hours:.0f}시간째" if hours < 48 else f"{hours / 24:.0f}일째"


def _josa(word: str, with_final: str, without_final: str) -> str:
    """받침에 맞는 조사를 붙인다. "위치은"처럼 틀린 문장이 LLM에게 가면 그대로 따라 쓴다."""
    last = word[-1:] if word else ""
    if "가" <= last <= "힣":
        has_final = (ord(last) - ord("가")) % 28 != 0
        return word + (with_final if has_final else without_final)
    return word + with_final
