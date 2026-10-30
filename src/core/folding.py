"""원본 측정값을 하루치 관측치로 접는 계산.

**도메인 지식이 없다.** 구간을 겹치지 않게 합치고, 하루 단위로 묶고, 기기가
겹칠 때 하나만 고르는 것 — 어떤 섹터의 데이터가 와도 같은 규칙이다.
출처(`source`)조차 인자로 받는다. 그게 무엇인지는 섹터가 안다.

여기 있는 이유는 백필과 수신구가 **같은 계산을 지나가야** 하기 때문이다.
계산이 두 군데 살면 경로에 따라 값이 달라지고, 원인이 데이터가 아니라 코드
위치라서 추적이 지독히 어려워진다 — 수면과 걸음수에서 각각 한 번씩 겪었다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.core.models import Observation


@dataclass(frozen=True)
class Sample:
    """집계 이전의 측정값 하나. 어느 기기가 언제 쟀는지까지 들고 있다."""

    start: datetime
    end: datetime
    value: float
    source: str = ""
    # 숫자 하나로 안 담기는 관측치가 있다 — 위치는 위도·경도 두 개다.
    # value 는 정확도 같은 품질 신호를 담고, 본체는 여기 싣는다.
    meta: Dict[str, Any] = field(default_factory=dict)


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


# 이보다 짧게 깼다가 다시 잠들면 같은 잠으로 본다. 워치는 밤사이 깬 구간을 따로 적고
# 그건 수면이 아니라 걸러지므로, 받은 수면 조각 사이에는 몇 분씩 틈이 있다.
# 2026-10 export 83밤 실측: 밤 안의 틈이 1시간 이하 256개, 넘는 것 9개로 깨끗이 갈린다.
SAME_SLEEP = timedelta(hours=1)

Span = Tuple[datetime, datetime]


def sleep_blocks(spans: Iterable[Span], same_sleep: timedelta = SAME_SLEEP) -> List[List[Span]]:
    """조각을 잠 덩어리로 묶는다. 덩어리마다 원래 조각을 그대로 담는다(조각 수가 품질 신호)."""
    blocks: List[List[Span]] = []
    block_end: Optional[datetime] = None
    for start, end in sorted(spans):
        if blocks and block_end is not None and start - block_end <= same_sleep:
            blocks[-1].append((start, end))
            block_end = max(block_end, end)
        else:
            blocks.append([(start, end)])
            block_end = end
    return blocks


def nights_from_spans(
    spans: Iterable[Span],
    kind: str,
    source: str,
    boundary_hour: int = 12,
    same_sleep: timedelta = SAME_SLEEP,
) -> List[Observation]:
    """구간들을 "하룻밤" 단위로 묶어 총 시간과 잠든·깬 시각을 낸다.

    밤 11시에 자서 아침 7시에 깨면 날짜가 두 개다. 정오를 경계로 삼아
    '정오~다음날 정오'를 한 밤으로 본다 — 깨어난 날짜에 그 밤이 귀속된다.
    (오후 낮잠은 다음 밤으로 딸려가는데, 이건 이 방식의 알려진 트레이드오프다.)

    **조각이 아니라 잠 덩어리를 밤에 배정한다.** 조각마다 따로 배정하면 새벽 4시 반에
    잠들어 오후 1시 반에 깬 잠이 정오에서 잘린다 — 정오 뒤 꼬리가 "다음 밤"이 되고,
    그 꼬리가 조각 셋을 넘으면 수면 급감 트리거가 "어젯밤 1.5시간"으로 울린다
    (2026-10-30 리뷰 재현, export 83밤 중 5밤의 깬 시각이 0.15~1.44시간 일렀다).

    잠든·깬 시각은 그 밤에서 가장 긴 덩어리의 시작과 끝이다. 첫 덩어리를 쓰면 낮잠이
    잠든 시각이 된다.

    **구간을 받는 이유**: 백필과 단축어가 같은 계산을 거치게 하려는 것이다.
    """
    by_night: Dict[datetime, List[List[Span]]] = {}
    for block in sleep_blocks(spans, same_sleep):
        night = _day_of(block[0][0] + timedelta(hours=24 - boundary_hour))
        by_night.setdefault(night, []).append(block)

    observations: List[Observation] = []
    for night, blocks in sorted(by_night.items()):
        pieces = [piece for block in blocks for piece in block]
        total = sum((end - start).total_seconds() for start, end in merge_spans(pieces))
        main = max(blocks, key=lambda block: max(e for _, e in block) - block[0][0])
        observations.append(
            Observation(
                source=source,
                kind=kind,
                value=round(total / 3600.0, 2),
                at=night,
                meta={
                    # 조각 수는 측정 품질 신호다. 워치는 정상 수면을 10~20조각으로
                    # 쪼개므로, 한두 개뿐이면 측정이 실패한 것이다.
                    "segments": len(pieces),
                    # 리듬(언제 자고 언제 깨나)의 재료. 합계만 남기면 다시 못 만든다.
                    "onset": main[0][0].isoformat(),
                    "wake": max(end for _, end in main).isoformat(),
                },
            )
        )
    return observations


def daily_first(samples: Iterable[Sample], kind: str, source: str) -> List[Observation]:
    """하루의 첫 값. 사건의 시각 자체가 값인 지표용."""
    return _pick_one(samples, kind, source, last=False)


def daily_last(samples: Iterable[Sample], kind: str, source: str) -> List[Observation]:
    """하루의 마지막 값.

    퇴근처럼 하루에 여러 번 일어날 수 있지만 의미 있는 건 마지막인 사건.
    평균을 내면 "3시와 7시 사이 어딘가"라는, 실제로 일어나지 않은 값이 나온다.
    """
    return _pick_one(samples, kind, source, last=True)


def _pick_one(samples: Iterable[Sample], kind: str, source: str, last: bool) -> List[Observation]:
    observations = []
    for day, day_samples in sorted(_bucket(samples).items()):
        ordered = sorted(day_samples, key=lambda s: s.start)
        chosen = ordered[-1] if last else ordered[0]
        observations.append(
            Observation(
                source=source,
                kind=kind,
                value=chosen.value,
                at=day,
                # 몇 번 일어났는지는 남긴다. 퇴근이 하루 세 번이면 그 자체가 신호다.
                # 키를 "at"으로 두면 응답 요약에서 관측치의 at 을 덮어쓴다 —
                # 저장은 멀쩡한데 응답만 거짓말하는 상태가 된다.
                meta={"samples": len(ordered), "event_at": chosen.start.isoformat()},
            )
        )
    return observations


def keep_points(samples: Iterable[Sample], kind: str, source: str) -> List[Observation]:
    """접지 않고 점을 그대로 남긴다.

    하루 단위로 접는 다른 fold 들과 달리 `at` 이 **표본의 실제 시각**이다.
    저장소 키가 (source, kind, at) 이라 같은 날 여러 점이 서로 덮어쓰지 않는다.

    위치가 이 모양을 요구한다 — "하루 평균 위치"는 아무 의미가 없고,
    "9시에 여기, 18시에 저기"가 낱낱이 남아야 패턴이 보인다.
    """
    return [
        Observation(
            source=source,
            kind=kind,
            value=sample.value,
            at=sample.start,
            meta=dict(sample.meta),
        )
        for sample in sorted(samples, key=lambda s: s.start)
    ]
