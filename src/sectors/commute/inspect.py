"""쌓인 위치를 훑어본다.

패턴을 **찾기 전에** 패턴이 있는지부터 봐야 한다. 트리거를 먼저 짜면
무엇을 감지할지 모르는 채로 감지기를 만드는 꼴이다.

    python -m src.sectors.commute.inspect

한 일: 가까운 점끼리 묶어 "자주 가는 곳"을 만들고, 시간대별로 어디서
출발했는지 센다. 군집이 안 생기면 아직 데이터가 모자란 것이다.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Sequence, Tuple

from src.core.config import load_settings
from src.core.models import Observation
from src.storage.sqlite import SQLiteStore

# 이 안쪽이면 같은 장소로 본다. 도시에서 건물 하나 남짓.
SAME_PLACE_METERS = 150.0
# GPS 오차가 이보다 크면 패턴 분석에 넣지 않는다. 수면의 조각 수와 같은 자리다.
MAX_ACCURACY_METERS = 200.0


def meters_between(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """두 좌표 사이 거리(m). 도시 규모에서는 평면 근사로 충분하다."""
    lat_mid = math.radians((a[0] + b[0]) / 2)
    dx = (b[1] - a[1]) * 111_320 * math.cos(lat_mid)
    dy = (b[0] - a[0]) * 110_540
    return math.hypot(dx, dy)


def coords(observation: Observation) -> Tuple[float, float] | None:
    lat, lon = observation.meta.get("lat"), observation.meta.get("lon")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def cluster(points: Sequence[Observation]) -> List[List[Observation]]:
    """가까운 점끼리 묶는다. 첫 점을 중심으로 삼는 단순한 방식."""
    groups: List[List[Observation]] = []
    for point in points:
        here = coords(point)
        if here is None:
            continue
        for group in groups:
            center = coords(group[0])
            if center and meters_between(center, here) <= SAME_PLACE_METERS:
                group.append(point)
                break
        else:
            groups.append([point])
    return sorted(groups, key=len, reverse=True)


def by_trigger(points: Sequence[Observation]) -> Dict[str, List[Observation]]:
    """어느 자동화가 보낸 점인지로 나눈다.

    같은 좌표라도 의미가 다르다 — 집을 나선 지점과 교통카드를 찍은 지점은
    전혀 다른 사실이고, 후자가 곧 "타는 곳"이다. 라벨이 없으면 6개 점 중
    무엇이 무엇인지 알 수 없다.
    """
    groups: Dict[str, List[Observation]] = {}
    for point in points:
        groups.setdefault(str(point.meta.get("trigger") or "(라벨 없음)"), []).append(point)
    return groups


def main() -> int:
    store = SQLiteStore(load_settings().db_path)
    since = datetime.now(timezone.utc) - timedelta(days=60)
    raw = list(store.recent("location", since))

    usable = [o for o in raw if coords(o) and o.value <= MAX_ACCURACY_METERS]
    dropped = len(raw) - len(usable)

    print(f"위치 기록 {len(raw)}개" + (f" (정확도 미달 {dropped}개 제외)" if dropped else ""))
    if not usable:
        print("\n아직 데이터가 없다. 아이폰 '떠날 때' 자동화를 걸면 쌓이기 시작한다.")
        return 0

    span = (usable[-1].at - usable[0].at).days + 1
    print(f"기간 {span}일  ({usable[0].at:%m-%d} ~ {usable[-1].at:%m-%d})\n")

    groups = cluster(usable)
    print(f"=== 자주 간 곳 {len(groups)}군데 ===")
    for i, group in enumerate(groups[:8], 1):
        lat, lon = coords(group[0]) or (0.0, 0.0)
        hours = Counter(o.at.hour for o in group)
        busiest = ", ".join(f"{h}시({n})" for h, n in hours.most_common(3))
        print(f"  {i}. {len(group):3}회  {lat:.4f},{lon:.4f}   주로 {busiest}")

    labelled = by_trigger(usable)
    if set(labelled) != {"(라벨 없음)"}:
        print("\n=== 자동화별 ===")
        for name, group in sorted(labelled.items(), key=lambda kv: -len(kv[1])):
            spots = cluster(group)
            top = coords(spots[0][0]) if spots else None
            where = f"  주로 {top[0]:.4f},{top[1]:.4f} ({len(spots[0])}회)" if top else ""
            print(f"  {name:14} {len(group):3}개, 장소 {len(spots)}군데{where}")

    print("\n=== 시간대별 출발 횟수 ===")
    by_hour: Dict[int, int] = defaultdict(int)
    for o in usable:
        by_hour[o.at.hour] += 1
    for hour in sorted(by_hour):
        print(f"  {hour:02d}시  {'█' * by_hour[hour]} {by_hour[hour]}")

    if len(groups) < 2 or span < 5:
        print("\n아직 패턴이라 부르기엔 이르다. 며칠 더 쌓고 다시 본다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
