"""회고를 손으로 돌린다.

    python -m app.reflect                                  # 지금 한 번 (최근 7일)
    python -m app.reflect --show                           # 돌리지 않고 나무만 본다
    python -m app.reflect --replay 2026-08-08 2026-11-02   # 그날들의 회고 시각마다 차례로(되감기)

서버는 매일 회고 시각(`REFLECT_AT`)에 스스로 돈다. 이건 손으로 돌려 볼 때와, 지난 기록으로 나무를
처음부터 키워 볼 때(되감기) 쓴다. 되감기는 그 DB 의 나무를 바꾼다 — 운영 DB 사본에서 먼저 돌린다
(`JARVIS_DB_PATH=사본 python -m app.reflect --replay ...`). 회고가 매일 돌면 나무가 얼마나 자주
바뀌는지를 몇 주 기다리지 않고 지난 석 달로 잰다.

섹터를 아는 파일은 `app/main.py` 하나뿐이라(불변식이 집행한다) 여기서는
섹터를 직접 열지 않고 조립 함수만 가져다 쓴다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import List

from app.main import REFLECT_AT, build_reflector
from src.brain.reflect import Reflection, Reflector
from src.core.clock import KST, local
from src.core.config import load_settings
from src.core.topics import standings
from src.storage.topics import SQLiteTopicStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")

# 나무 모양을 바꾸는 연산. 이게 매일 쏟아지면 나무를 믿을 수 없다.
RESHAPING = ("rename", "move", "merge", "retire")


def show(now: datetime) -> None:
    store = SQLiteTopicStore(load_settings().db_path)
    board = standings(store.load(), store.grounds(), now)
    if not board:
        print("아직 아는 게 없다.")
        return
    notes = store.latest_notes(1)
    for s in board:
        pad = "  " * s.depth
        last = f" · 마지막 {local(s.last):%m-%d}" if s.last else ""
        print(
            f"{pad}#{s.node.id} {s.node.name} — {s.trend.value}"
            f" · 근거 {s.count}개({s.days}일){last}"
        )
        for note in notes.get(s.node.id, []):
            print(f"{pad}   메모: {note.text[:100]}")


def report(result: Reflection, verbose: bool) -> None:
    for line in result.retired:
        print(f"  접음  {line}")
    if not verbose:
        return
    for line in result.applied:
        print(f"  적용  {line}")
    for line in result.rejected:
        print(f"  기각  {line}")
    for line in result.notices:
        print(f"  고침  {line}")


async def replay(reflector: Reflector, start: date, end: date, verbose: bool) -> None:
    """그날들의 회고 시각을 차례로 '지금'으로 삼아 돈다. 흔적은 그때까지 있던 것만 본다."""
    first = datetime.combine(start, REFLECT_AT, tzinfo=KST)
    day = start
    while day <= end:
        now = datetime.combine(day, REFLECT_AT, tzinfo=KST)
        began = time.monotonic()
        try:
            result = await reflector.run_once(now)
        except Exception as exc:  # 하루가 실패해도 되감기는 이어간다 — 실패도 기록에 남았다
            print(f"{day:%m-%d} 실패: {exc}")
        else:
            print(
                f"{day:%m-%d} 흔적 {result.considered:4d}줄 → 변경 {len(result.applied):2d} · "
                f"메모 {result.notes:2d} · 기각 {len(result.rejected):2d}"
                f" · 접음 {len(result.retired)}"
                f"  ({result.prompt_chars:,}자, {time.monotonic() - began:.0f}초)"
            )
            report(result, verbose)
        day += timedelta(days=1)

    changes = reflector.topics.changes(since=first)
    ops = Counter(c.op for c in changes)
    days = (end - start).days + 1
    reshaped = Counter(local(c.at).date() for c in changes if c.op in RESHAPING)
    alive = [n for n in reflector.topics.load().live()]
    print(f"\n{days}일 되감기 — 살아 있는 주제 {len(alive)}개")
    print("연산: " + ", ".join(f"{op} {n}" for op, n in ops.most_common()))
    spread: List[int] = sorted(reshaped.values(), reverse=True)
    print(f"모양 바꾼 날 {len(reshaped)}/{days}일" + (f", 많은 날 {spread[:5]}" if spread else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="흔적을 훑어 주제 나무를 가꾼다")
    parser.add_argument("--days", type=int, default=7, help="며칠치 흔적을 볼지")
    parser.add_argument("--show", action="store_true", help="돌리지 않고 지금 나무만 본다")
    parser.add_argument(
        "--replay", nargs=2, metavar=("시작", "끝"), help="그날들을 하루씩 되감는다(YYYY-MM-DD)"
    )
    parser.add_argument("--verbose", action="store_true", help="되감기에서 변경·기각을 다 찍는다")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    if args.show:
        show(now)
        return

    reflector = build_reflector(load_settings())
    reflector.lookback = timedelta(days=args.days)
    if args.replay:
        start, end = (date.fromisoformat(d) for d in args.replay)
        asyncio.run(replay(reflector, start, end, args.verbose))
        show(datetime.combine(end, REFLECT_AT, tzinfo=KST))
        return

    result = asyncio.run(reflector.run_once(now))
    print(
        f"\n흔적 {result.considered}줄 + 하루 요약 {result.digested}줄"
        f" → 변경 {len(result.applied)}건, 메모 {result.notes}건"
    )
    report(result, verbose=True)
    show(now)


if __name__ == "__main__":
    main()
