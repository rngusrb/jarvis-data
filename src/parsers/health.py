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


def nightly_sleep(records: Iterable[HealthRecord], boundary_hour: int = 12) -> List[Observation]:
    """수면 조각들을 "하룻밤" 단위로 묶어 총 수면 시간을 낸다.

    밤 11시에 자서 아침 7시에 깨면 날짜가 두 개다. 정오를 경계로 삼아
    '정오~다음날 정오'를 한 밤으로 본다 — 깨어난 날짜에 그 밤이 귀속된다.
    (낮잠은 다음 밤으로 딸려가는데, 이건 이 방식의 알려진 트레이드오프다.)
    """
    by_night: Dict[datetime, List[Tuple[datetime, datetime]]] = {}

    for record in records:
        if record.type != SLEEP_TYPE or not record.value.startswith(ASLEEP_PREFIX):
            continue
        night = (record.start + timedelta(hours=24 - boundary_hour)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        by_night.setdefault(night, []).append((record.start, record.end))

    observations: List[Observation] = []
    for night, spans in sorted(by_night.items()):
        total = sum((end - start).total_seconds() for start, end in merge_spans(spans))
        observations.append(
            Observation(
                source=SOURCE,
                kind="sleep_hours",
                value=round(total / 3600.0, 2),
                at=night,
                meta={"segments": len(spans)},
            )
        )
    return observations


def daily_total(records: Iterable[HealthRecord], record_type: str, kind: str) -> List[Observation]:
    """걸음수처럼 하루 단위로 합산하는 수치를 낸다."""
    by_day: Dict[datetime, float] = {}

    for record in records:
        if record.type != record_type:
            continue
        amount = record.numeric_value
        if amount is None:
            continue
        day = record.start.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day[day] = by_day.get(day, 0.0) + amount

    return [
        Observation(source=SOURCE, kind=kind, value=round(total, 2), at=day)
        for day, total in sorted(by_day.items())
    ]


def daily_average(
    records: Iterable[HealthRecord], record_type: str, kind: str
) -> List[Observation]:
    """심박수처럼 하루 단위로 평균을 내는 수치를 낸다."""
    by_day: Dict[datetime, List[float]] = {}

    for record in records:
        if record.type != record_type:
            continue
        amount = record.numeric_value
        if amount is None:
            continue
        day = record.start.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day.setdefault(day, []).append(amount)

    return [
        Observation(
            source=SOURCE,
            kind=kind,
            value=round(sum(values) / len(values), 2),
            at=day,
            meta={"samples": len(values)},
        )
        for day, values in sorted(by_day.items())
    ]


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
