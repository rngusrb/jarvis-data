"""원본 측정값을 하루치 관측치로 접는 계산.

**도메인 지식이 없다.** 구간을 겹치지 않게 합치고, 하루 단위로 묶고, 기기가
겹칠 때 하나만 고르는 것 — 어떤 섹터의 데이터가 와도 같은 규칙이다.
출처(`source`)조차 인자로 받는다. 그게 무엇인지는 섹터가 안다.

여기 있는 이유는 백필과 수신구가 **같은 계산을 지나가야** 하기 때문이다.
계산이 두 군데 살면 경로에 따라 값이 달라지고, 원인이 데이터가 아니라 코드
위치라서 추적이 지독히 어려워진다 — 수면과 걸음수에서 각각 한 번씩 겪었다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Sequence, Tuple

from src.core.models import Observation


@dataclass(frozen=True)
class Sample:
    """집계 이전의 측정값 하나. 어느 기기가 언제 쟀는지까지 들고 있다."""

    start: datetime
    end: datetime
    value: float
    source: str = ""


def is_wrist(source: str) -> bool:
    """손목에서 잰 것인지. 겹칠 때 워치를 우선한다 — 몸에 붙어 있으니 덜 놓친다."""
    lowered = source.lower()
    return "watch" in lowered or "워치" in source


def merge_spans(spans: Iterable[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    """겹치는 시간 구간을 하나로 합친다.

    두 기기가 같은 잠을 각자 기록하기 때문에 필요하다.
    그냥 더하면 8시간 잔 밤이 13시간으로 부풀어난다.
    """
    merged: List[Tuple[datetime, datetime]] = []
    for start, end in sorted(spans, key=lambda s: s[0]):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _day_of(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _bucket(samples: Iterable[Sample]) -> Dict[datetime, List[Sample]]:
    by_day: Dict[datetime, List[Sample]] = {}
    for sample in samples:
        by_day.setdefault(_day_of(sample.start), []).append(sample)
    return by_day


def _sum_one_day(samples: Sequence[Sample]) -> float:
    """겹치는 기기 기록을 걷어내고 더한다.

    아이폰과 워치는 같은 걸음을 각자 센다. 그냥 더하면 8천 걸음이 1만 5천이 된다
    — 애플 건강 앱이 조용히 해결해주던 일이다. 같은 방식으로 **시간 구간마다
    소스를 하나만** 채택한다. 워치를 먼저 깔고, 워치가 비워둔 구간만 다른 기기가
    채운다. 일부만 겹치는 기록은 비어 있던 시간 비율만큼만 인정한다.
    """
    if len({s.source for s in samples}) < 2:
        # 소스가 하나면 중복될 일이 없다. 소스 정보 없이 들어온 경로도 여기로 온다.
        return sum(s.value for s in samples)

    taken: List[Tuple[datetime, datetime]] = []
    total = 0.0

    for sample in sorted(samples, key=lambda s: (not is_wrist(s.source), s.start)):
        span = (sample.end - sample.start).total_seconds()
        if span <= 0:
            if not any(lo <= sample.start <= hi for lo, hi in taken):
                total += sample.value
                taken.append((sample.start, sample.end))
            continue

        covered = 0.0
        for lo, hi in taken:
            overlap_lo, overlap_hi = max(lo, sample.start), min(hi, sample.end)
            if overlap_hi > overlap_lo:
                covered += (overlap_hi - overlap_lo).total_seconds()

        total += sample.value * max(0.0, span - covered) / span
        taken.append((sample.start, sample.end))

    return total


def daily_sum(samples: Iterable[Sample], kind: str, source: str) -> List[Observation]:
    """걸음수처럼 하루치를 더하는 지표. 기기 중복은 걷어낸다."""
    observations = []
    for day, day_samples in sorted(_bucket(samples).items()):
        sources = sorted({s.source for s in day_samples if s.source})
        observations.append(
            Observation(
                source=source,
                kind=kind,
                value=round(_sum_one_day(day_samples), 2),
                at=day,
                # 표본 수는 값이 이상할 때 원인을 가르는 첫 단서다. 하루 걸음수가
                # 9만이면, 표본이 5개인지 900개인지에 따라 원인이 완전히 달라진다.
                meta={"samples": len(day_samples), "sources": sources},
            )
        )
    return observations


def daily_mean(samples: Iterable[Sample], kind: str, source: str) -> List[Observation]:
    """심박처럼 하루치를 평균 내는 지표.

    소스가 여럿이면 표본이 가장 많은 기기 하나만 쓴다. 기기마다 재는 시점이
    달라서 섞어 평균 내면 어느 쪽도 아닌 값이 된다.
    """
    observations = []
    for day, day_samples in sorted(_bucket(samples).items()):
        chosen = day_samples
        sources = sorted({s.source for s in day_samples if s.source})
        if len(sources) > 1:
            best = max(sources, key=lambda name: sum(1 for s in day_samples if s.source == name))
            chosen = [s for s in day_samples if s.source == best]
        observations.append(
            Observation(
                source=source,
                kind=kind,
                value=round(sum(s.value for s in chosen) / len(chosen), 2),
                at=day,
                meta={"samples": len(chosen), "sources": sources},
            )
        )
    return observations


def nights_from_spans(
    spans: Iterable[Tuple[datetime, datetime]],
    kind: str,
    source: str,
    boundary_hour: int = 12,
) -> List[Observation]:
    """구간들을 "하룻밤" 단위로 묶어 총 시간을 낸다.

    밤 11시에 자서 아침 7시에 깨면 날짜가 두 개다. 정오를 경계로 삼아
    '정오~다음날 정오'를 한 밤으로 본다 — 깨어난 날짜에 그 밤이 귀속된다.
    (낮잠은 다음 밤으로 딸려가는데, 이건 이 방식의 알려진 트레이드오프다.)

    **구간을 받는 이유**: 백필과 단축어가 같은 계산을 거치게 하려는 것이다.
    """
    by_night: Dict[datetime, List[Tuple[datetime, datetime]]] = {}
    for start, end in spans:
        night = _day_of(start + timedelta(hours=24 - boundary_hour))
        by_night.setdefault(night, []).append((start, end))

    observations: List[Observation] = []
    for night, night_spans in sorted(by_night.items()):
        total = sum((end - start).total_seconds() for start, end in merge_spans(night_spans))
        observations.append(
            Observation(
                source=source,
                kind=kind,
                value=round(total / 3600.0, 2),
                at=night,
                # 조각 수는 측정 품질 신호다. 워치는 정상 수면을 10~20조각으로
                # 쪼개므로, 한두 개뿐이면 측정이 실패한 것이다.
                meta={"segments": len(night_spans)},
            )
        )
    return observations
