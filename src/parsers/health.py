"""Apple Health 내보내기(export.xml) 파서.

건강 앱이 뱉는 XML은 수백MB~GB까지 간다. 통째로 읽으면 메모리가 터지므로
iterparse로 흘려보내면서 필요한 Record만 뽑는다.

XML 구조는 대략 이렇다:

    <HealthData>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count"
              startDate="2026-08-18 09:00:00 +0900" endDate="..." value="120"/>
      <Record type="HKCategoryTypeIdentifierSleepAnalysis"
              value="HKCategoryValueSleepAnalysisAsleepCore" startDate=... endDate=.../>
    </HealthData>
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from src.core.models import Observation

SOURCE = "apple_health"

SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
STEP_TYPE = "HKQuantityTypeIdentifierStepCount"
HEART_RATE_TYPE = "HKQuantityTypeIdentifierHeartRate"

# iOS 16부터 수면이 Core/Deep/REM으로 쪼개진다. InBed(누워만 있음)와
# Awake(중간에 깸)는 실제 수면이 아니므로 뺀다 — 이걸 포함하면 수면 시간이 부풀어난다.
ASLEEP_PREFIX = "HKCategoryValueSleepAnalysisAsleep"

APPLE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"


@dataclass(frozen=True)
class HealthRecord:
    """XML의 <Record> 하나를 그대로 옮긴 것. 아직 해석하지 않은 원본이다."""

    type: str
    value: str
    unit: str
    start: datetime
    end: datetime
    source_name: str

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    @property
    def numeric_value(self) -> Optional[float]:
        try:
            return float(self.value)
        except ValueError:
            # 수면처럼 값이 문자열 카테고리인 레코드가 있다.
            return None


def _parse_date(raw: str) -> datetime:
    return datetime.strptime(raw, APPLE_DATE_FORMAT)


def iter_records(path: Path, types: Optional[Set[str]] = None) -> Iterator[HealthRecord]:
    """export.xml을 흘려보내며 Record를 하나씩 뱉는다.

    ``types``를 주면 그 타입만 통과시킨다. 파일에 수백만 개 레코드가 있고
    대부분은 관심 밖이라, 필터를 여기서 거는 게 훨씬 싸다.
    """
    context = ET.iterparse(str(path), events=("start", "end"))
    _, root = next(context)

    for event, elem in context:
        if event != "end" or elem.tag != "Record":
            continue

        record_type = elem.get("type", "")
        if types is None or record_type in types:
            start_raw = elem.get("startDate")
            end_raw = elem.get("endDate")
            if start_raw and end_raw:
                yield HealthRecord(
                    type=record_type,
                    value=elem.get("value", ""),
                    unit=elem.get("unit", ""),
                    start=_parse_date(start_raw),
                    end=_parse_date(end_raw),
                    source_name=elem.get("sourceName", ""),
                )

        # 처리한 요소를 즉시 버린다. 이걸 안 하면 iterparse를 써도
        # 트리가 루트 밑에 계속 쌓여서 결국 메모리가 터진다.
        elem.clear()
        root.clear()


def merge_spans(spans: Iterable[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    """겹치는 시간 구간을 하나로 합친다.

    애플워치와 아이폰이 같은 잠을 각자 기록하기 때문에 필요하다.
    그냥 더하면 8시간 잔 밤이 13시간으로 부풀어난다.
    """
    ordered = sorted(spans, key=lambda s: s[0])
    merged: List[Tuple[datetime, datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def nights_from_spans(
    spans: Iterable[Tuple[datetime, datetime]],
    boundary_hour: int = 12,
    kind: str = "sleep_hours",
) -> List[Observation]:
    """수면 구간들을 "하룻밤" 단위로 묶어 총 수면 시간을 낸다.

    밤 11시에 자서 아침 7시에 깨면 날짜가 두 개다. 정오를 경계로 삼아
    '정오~다음날 정오'를 한 밤으로 본다 — 깨어난 날짜에 그 밤이 귀속된다.
    (낮잠은 다음 밤으로 딸려가는데, 이건 이 방식의 알려진 트레이드오프다.)

    **HealthRecord가 아니라 구간을 받는 이유**: 백필(export.xml)과 단축어가
    같은 계산을 거치게 하려는 것이다. 계산이 두 군데 있으면 경로에 따라 값이
    달라지고, 그건 나중에 원인을 찾기 지독히 어려운 종류의 버그가 된다.
    """
    by_night: Dict[datetime, List[Tuple[datetime, datetime]]] = {}

    for start, end in spans:
        night = (start + timedelta(hours=24 - boundary_hour)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        by_night.setdefault(night, []).append((start, end))

    observations: List[Observation] = []
    for night, night_spans in sorted(by_night.items()):
        total = sum((end - start).total_seconds() for start, end in merge_spans(night_spans))
        observations.append(
            Observation(
                source=SOURCE,
                kind=kind,
                value=round(total / 3600.0, 2),
                at=night,
                # 조각 수는 측정 품질 신호다. 워치는 정상 수면을 10~20조각으로
                # 쪼개므로, 한두 개뿐이면 측정이 실패한 것이다.
                meta={"segments": len(night_spans)},
            )
        )
    return observations


def nightly_sleep(records: Iterable[HealthRecord], boundary_hour: int = 12) -> List[Observation]:
    """export.xml에서 읽은 레코드를 밤 단위 수면으로 집계한다."""
    spans = [
        (record.start, record.end)
        for record in records
        if record.type == SLEEP_TYPE and record.value.startswith(ASLEEP_PREFIX)
    ]
    return nights_from_spans(spans, boundary_hour=boundary_hour)


@dataclass(frozen=True)
class Sample:
    """집계 이전의 측정값 하나. 어느 기기가 언제 쟀는지까지 들고 있다."""

    start: datetime
    end: datetime
    value: float
    source: str = ""


def is_wrist(source: str) -> bool:
    """손목에서 잰 것인지. 겹칠 때 워치를 우선한다 — 몸에 붙어 있으니 더 정확하다."""
    lowered = source.lower()
    return "watch" in lowered or "워치" in source


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
    — 수면에서 겪은 것과 같은 문제이고, 애플 건강 앱이 조용히 해결해주던 일이다.

    건강 앱과 같은 방식으로 **시간 구간마다 소스를 하나만** 채택한다. 워치를
    먼저 깔고, 워치가 비워둔 구간만 다른 기기가 채운다. 일부만 겹치는 기록은
    비어 있던 시간 비율만큼만 인정한다.
    """
    if len({s.source for s in samples}) < 2:
        # 소스가 하나면 중복될 일이 없다. 소스 정보 없이 들어온 경로도 여기로 온다.
        return sum(s.value for s in samples)

    ordered = sorted(samples, key=lambda s: (not is_wrist(s.source), s.start))
    taken: List[Tuple[datetime, datetime]] = []
    total = 0.0

    for sample in ordered:
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


def daily_sum(samples: Iterable[Sample], kind: str) -> List[Observation]:
    """걸음수처럼 하루치를 더하는 지표. 기기 중복은 걷어낸다."""
    observations = []
    for day, day_samples in sorted(_bucket(samples).items()):
        sources = sorted({s.source for s in day_samples if s.source})
        observations.append(
            Observation(
                source=SOURCE,
                kind=kind,
                value=round(_sum_one_day(day_samples), 2),
                at=day,
                meta={"samples": len(day_samples), "sources": sources},
            )
        )
    return observations


def daily_mean(samples: Iterable[Sample], kind: str) -> List[Observation]:
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
                source=SOURCE,
                kind=kind,
                value=round(sum(s.value for s in chosen) / len(chosen), 2),
                at=day,
                meta={"samples": len(chosen), "sources": sources},
            )
        )
    return observations


def _samples(records: Iterable[HealthRecord], record_type: str) -> List[Sample]:
    found = []
    for record in records:
        if record.type != record_type:
            continue
        amount = record.numeric_value
        if amount is not None:
            found.append(
                Sample(
                    start=record.start,
                    end=record.end,
                    value=amount,
                    source=record.source_name,
                )
            )
    return found


def daily_total(records: Iterable[HealthRecord], record_type: str, kind: str) -> List[Observation]:
    """export.xml 레코드를 하루 합계로 집계한다."""
    return daily_sum(_samples(records, record_type), kind)


def daily_average(
    records: Iterable[HealthRecord], record_type: str, kind: str
) -> List[Observation]:
    """export.xml 레코드를 하루 평균으로 집계한다."""
    return daily_mean(_samples(records, record_type), kind)


def parse_export(path: Path) -> Sequence[Observation]:
    """export.xml 한 번 읽어서 관심 있는 관측치를 전부 뽑는다.

    파일이 크므로 **한 번만 훑는다**. 타입별로 여러 번 여는 건 피한다.
    """
    wanted = {SLEEP_TYPE, STEP_TYPE, HEART_RATE_TYPE}
    records = list(iter_records(path, types=wanted))

    observations: List[Observation] = []
    observations.extend(nightly_sleep(records))
    observations.extend(daily_total(records, STEP_TYPE, "step_count"))
    observations.extend(daily_average(records, HEART_RATE_TYPE, "heart_rate_avg"))
    return observations
