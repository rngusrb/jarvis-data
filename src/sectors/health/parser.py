"""Apple Health 내보내기(export.xml) 파서 — health 섹터 전용.

건강 앱이 뱉는 XML은 수백MB~GB까지 간다. 통째로 읽으면 메모리가 터지므로
iterparse로 흘려보내면서 필요한 Record만 뽑는다.

접는 계산은 여기 없다 — `src/core/folding`에 있고, 수신구도 같은 것을 쓴다.
이 파일이 아는 것은 **애플이 XML을 어떻게 쓰는가**뿐이다.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Set

from src.core.folding import Sample, daily_mean, daily_sum, nights_from_spans
from src.core.models import Observation

SOURCE = "apple_health"

SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
STEP_TYPE = "HKQuantityTypeIdentifierStepCount"
RESTING_HEART_TYPE = "HKQuantityTypeIdentifierRestingHeartRate"

# iOS 16부터 수면이 Core/Deep/REM으로 쪼개진다. InBed(누워만 있음)와
# Awake(중간에 깸)는 실제 수면이 아니므로 뺀다.
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


def nightly_sleep(records: Iterable[HealthRecord], kind: str = "sleep_hours") -> List[Observation]:
    """수면 레코드를 밤 단위로 집계한다."""
    spans = [
        (record.start, record.end)
        for record in records
        if record.type == SLEEP_TYPE and record.value.startswith(ASLEEP_PREFIX)
    ]
    return nights_from_spans(spans, kind=kind, source=SOURCE)


def daily_total(records: Iterable[HealthRecord], record_type: str, kind: str) -> List[Observation]:
    return daily_sum(_samples(records, record_type), kind=kind, source=SOURCE)


def daily_average(
    records: Iterable[HealthRecord], record_type: str, kind: str
) -> List[Observation]:
    return daily_mean(_samples(records, record_type), kind=kind, source=SOURCE)


def parse_export(path: Path) -> Sequence[Observation]:
    """export.xml 한 번 읽어서 관심 있는 관측치를 전부 뽑는다.

    파일이 크므로 **한 번만 훑는다**. 타입별로 여러 번 여는 건 피한다.
    """
    wanted = {SLEEP_TYPE, STEP_TYPE, RESTING_HEART_TYPE}
    records = list(iter_records(path, types=wanted))

    observations: List[Observation] = []
    observations.extend(nightly_sleep(records))
    observations.extend(daily_total(records, STEP_TYPE, "step_count"))
    observations.extend(daily_average(records, RESTING_HEART_TYPE, "resting_heart_rate"))
    return observations
