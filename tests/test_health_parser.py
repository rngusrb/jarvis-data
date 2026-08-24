from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.parsers.health import (
    HEART_RATE_TYPE,
    STEP_TYPE,
    daily_average,
    daily_total,
    iter_records,
    merge_spans,
    nightly_sleep,
    parse_export,
)

KST = timezone(timedelta(hours=9))

# 실제 export.xml을 축소한 것. 애플워치와 아이폰이 같은 잠을 겹쳐 기록한 상황,
# InBed/Awake 구간, 자정을 넘는 수면이 모두 들어 있다.
FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="ko_KR">
 <ExportDate value="2026-08-19 10:00:00 +0900"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Apple Watch"
   value="HKCategoryValueSleepAnalysisAsleepCore"
   startDate="2026-08-18 23:00:00 +0900" endDate="2026-08-19 03:00:00 +0900"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="iPhone"
   value="HKCategoryValueSleepAnalysisAsleepUnspecified"
   startDate="2026-08-19 01:00:00 +0900" endDate="2026-08-19 07:00:00 +0900"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Apple Watch"
   value="HKCategoryValueSleepAnalysisInBed"
   startDate="2026-08-18 22:00:00 +0900" endDate="2026-08-18 23:00:00 +0900"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Apple Watch"
   value="HKCategoryValueSleepAnalysisAwake"
   startDate="2026-08-19 03:00:00 +0900" endDate="2026-08-19 03:30:00 +0900"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count"
   startDate="2026-08-19 09:00:00 +0900" endDate="2026-08-19 09:10:00 +0900" value="120"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count"
   startDate="2026-08-19 10:00:00 +0900" endDate="2026-08-19 10:10:00 +0900" value="80"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" unit="count/min"
   startDate="2026-08-19 09:00:00 +0900" endDate="2026-08-19 09:00:05 +0900" value="70"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" unit="count/min"
   startDate="2026-08-19 09:30:00 +0900" endDate="2026-08-19 09:30:05 +0900" value="80"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="iPhone" unit="kg"
   startDate="2026-08-19 08:00:00 +0900" endDate="2026-08-19 08:00:00 +0900" value="70"/>
</HealthData>
"""


def _fixture(tmp_path: Path) -> Path:
    path = tmp_path / "export.xml"
    path.write_text(FIXTURE, encoding="utf-8")
    return path


def test_원하는_타입만_읽는다(tmp_path: Path) -> None:
    records = list(iter_records(_fixture(tmp_path), types={STEP_TYPE}))
    assert len(records) == 2
    assert all(r.type == STEP_TYPE for r in records)


def test_필터_없으면_전부_읽는다(tmp_path: Path) -> None:
    assert len(list(iter_records(_fixture(tmp_path)))) == 9


def test_겹치는_구간을_합친다() -> None:
    base = datetime(2026, 8, 18, 23, 0, tzinfo=KST)
    spans = [
        (base, base + timedelta(hours=4)),
        (base + timedelta(hours=2), base + timedelta(hours=8)),
    ]
    assert merge_spans(spans) == [(base, base + timedelta(hours=8))]


def test_안_겹치면_그대로_둔다() -> None:
    base = datetime(2026, 8, 18, 23, 0, tzinfo=KST)
    spans = [
        (base, base + timedelta(hours=1)),
        (base + timedelta(hours=3), base + timedelta(hours=4)),
    ]
    assert len(merge_spans(spans)) == 2


def test_워치와_아이폰_중복_기록이_부풀지_않는다(tmp_path: Path) -> None:
    """워치 4시간 + 아이폰 6시간을 그냥 더하면 10시간이 된다. 실제로는 8시간이다."""
    records = list(iter_records(_fixture(tmp_path)))
    nights = nightly_sleep(records)
    assert len(nights) == 1
    assert nights[0].value == 8.0


def test_누워만_있거나_깬_시간은_수면이_아니다(tmp_path: Path) -> None:
    records = list(iter_records(_fixture(tmp_path)))
    nights = nightly_sleep(records)
    # InBed 1시간과 Awake 30분이 포함됐다면 8시간을 넘었을 것이다.
    assert nights[0].value == 8.0


def test_자정을_넘긴_잠은_깨어난_날로_묶인다(tmp_path: Path) -> None:
    records = list(iter_records(_fixture(tmp_path)))
    night = nightly_sleep(records)[0]
    assert night.at.date() == datetime(2026, 8, 19).date()


def test_걸음수는_하루_단위로_합산된다(tmp_path: Path) -> None:
    records = list(iter_records(_fixture(tmp_path)))
    steps = daily_total(records, STEP_TYPE, "step_count")
    assert len(steps) == 1
    assert steps[0].value == 200.0


def test_심박수는_하루_단위로_평균낸다(tmp_path: Path) -> None:
    records = list(iter_records(_fixture(tmp_path)))
    heart = daily_average(records, HEART_RATE_TYPE, "heart_rate_avg")
    assert heart[0].value == 75.0
    assert heart[0].meta["samples"] == 2


def test_한_번_읽어서_전부_뽑는다(tmp_path: Path) -> None:
    observations = parse_export(_fixture(tmp_path))
    kinds = {o.kind for o in observations}
    assert kinds == {"sleep_hours", "step_count", "heart_rate_avg"}
