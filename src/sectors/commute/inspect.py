"""쌓인 위치를 훑어본다.

패턴을 **찾기 전에** 패턴이 있는지부터 봐야 한다. 트리거를 먼저 짜면
무엇을 감지할지 모르는 채로 감지기를 만드는 꼴이다.

    python -m src.sectors.commute.inspect

한 일: 장소에 아직 안 붙은 사건을 붙이고(`places` 표), 장소마다 역할 후보(집·타는 곳)와
시간대를 보여준다. 장소가 안 생기면 아직 데이터가 모자란 것이다.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Sequence, Tuple

from src.core.clock import local
from src.core.config import load_settings
from src.core.days import set_holidays
from src.core.models import Event
from src.sectors.commute.places import roles
from src.storage.days import SQLiteHolidays
from src.storage.events import SQLiteEventStore
from src.storage.places import SQLitePlaceStore

# GPS 오차가 이보다 크면 패턴 분석에 넣지 않는다. 수면의 조각 수와 같은 자리다.
MAX_ACCURACY_METERS = 200.0


def coords(event: Event) -> Tuple[float, float] | None:
    if event.lat is None or event.lon is None:
        return None
    return event.lat, event.lon


def accurate(event: Event) -> bool:
    """정확도를 모르면(단축어는 안 보낸다) 쓴다. 알고 있고 크면 뺀다."""
    return float(event.meta.get("accuracy") or 0) <= MAX_ACCURACY_METERS


def by_trigger(points: Sequence[Event]) -> Dict[str, List[Event]]:
    """어느 자동화가 보낸 점인지로 나눈다.

    같은 좌표라도 의미가 다르다 — 집을 나선 지점과 교통카드를 찍은 지점은
    전혀 다른 사실이고, 후자가 곧 "타는 곳"이다. 라벨이 없으면 6개 점 중
    무엇이 무엇인지 알 수 없다.
    """
    groups: Dict[str, List[Event]] = {}
    for point in points:
        groups.setdefault(point.label or "(라벨 없음)", []).append(point)
    return groups


def main() -> int:
    db = load_settings().db_path
    # 공휴일 표는 프로세스에 하나다(`core/days`) — 서버처럼 받아 둔 표를 올려야 공휴일을 평일로 안
    # 센다.
    set_holidays(SQLiteHolidays(db).load())
    placed = SQLitePlaceStore(db).assign_new()
    # 역할은 사흘 이상 모여야 나온다. 폰 데이터가 띄엄띄엄 와서(52일 중 9일) 60일로는
    # 모자랐다 — 반년을 본다.
    since = datetime.now(timezone.utc) - timedelta(days=180)
    raw = SQLiteEventStore(db).recent("location", since)
    usable = [e for e in raw if coords(e) and accurate(e)]
    dropped = len(raw) - len(usable)

    print(f"위치 기록 {len(raw)}개" + (f" (정확도 미달 {dropped}개 제외)" if dropped else ""))
    if placed:
        print(f"새로 장소에 붙임 {placed}개")
    if not usable:
        print("\n아직 데이터가 없다. 아이폰 '떠날 때' 자동화를 걸면 쌓이기 시작한다.")
        return 0

    span = (usable[-1].at - usable[0].at).days + 1
    print(f"기간 {span}일  ({local(usable[0].at):%m-%d} ~ {local(usable[-1].at):%m-%d})\n")

    places = {p.id: p for p in SQLitePlaceStore(db).all()}
    by_place: Dict[int, List[Event]] = defaultdict(list)
    for e in usable:
        if e.place_id is not None:
            by_place[e.place_id].append(e)
    named: Dict[int, List[str]] = defaultdict(list)
    for role in roles(usable):
        named[role.place_id].append(f"{role.name}({role.days}/{role.of}일)")

    print(f"=== 자주 간 곳 {len(by_place)}군데 ===")
    for place_id, group in sorted(by_place.items(), key=lambda kv: -len(kv[1]))[:8]:
        place = places[place_id]
        # 사건 시각은 UTC 로 저장된다. 시간대별로 셀 땐 한국 시각으로.
        hours = Counter(local(e.at).hour for e in group)
        busiest = ", ".join(f"{h}시({n})" for h, n in hours.most_common(3))
        candidates = ", ".join(named.get(place_id, [])) or "-"
        where = f"{place.lat:.4f},{place.lon:.4f}"
        print(f"  #{place_id:<3} {len(group):3}회  {where}   주로 {busiest}")
        print(f"        역할 후보: {candidates}")

    labelled = by_trigger(usable)
    if set(labelled) != {"(라벨 없음)"}:
        print("\n=== 자동화별 ===")
        for name, group in sorted(labelled.items(), key=lambda kv: -len(kv[1])):
            spots = Counter(e.place_id for e in group)
            top, count = spots.most_common(1)[0]
            print(f"  {name:14} {len(group):3}개, 장소 {len(spots)}군데  주로 #{top} ({count}회)")

    print("\n=== 시간대별 출발 횟수 ===")
    by_hour: Dict[int, int] = defaultdict(int)
    for e in usable:
        by_hour[local(e.at).hour] += 1
    for hour in sorted(by_hour):
        print(f"  {hour:02d}시  {'█' * by_hour[hour]} {by_hour[hour]}")

    if len(by_place) < 2 or span < 5:
        print("\n아직 패턴이라 부르기엔 이르다. 며칠 더 쌓고 다시 본다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
