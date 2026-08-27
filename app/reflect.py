"""회고를 한 번 돌린다.

    python -m app.reflect              # 최근 7일 흔적을 훑는다
    python -m app.reflect --days 14
    python -m app.reflect --show       # 돌리지 않고 지금 믿음만 본다

아직 자동으로 안 돈다. 주기 루프에 걸기 전에 몇 번 손으로 돌려보고 품질을
확인하는 단계다 — 걸음수도 다 만들고 나서 접었다.

섹터를 아는 파일은 `app/main.py` 하나뿐이라(불변식이 집행한다) 여기서는
섹터를 직접 열지 않고 조립 함수만 가져다 쓴다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.main import build_reflector
from src.core.config import load_settings
from src.storage.beliefs import SQLiteBeliefStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")

KST = timezone(timedelta(hours=9))


def show(now: datetime) -> None:
    beliefs = SQLiteBeliefStore(load_settings().db_path).all()
    if not beliefs:
        print("아직 아는 게 없다.")
        return
    for belief in beliefs:
        age = (now - belief.first_seen).days
        head = f"{belief.kind}]  확신 {belief.confidence:.1f}  {belief.aged(now).value}"
        print(f"\n[{head}  {age}일째")
        print(f"  {belief.value}")
        for item in belief.evidence[:5]:
            print(f"    · {item[:76]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="흔적을 훑어 믿음을 갱신한다")
    parser.add_argument("--days", type=int, default=7, help="며칠치 흔적을 볼지")
    parser.add_argument("--show", action="store_true", help="돌리지 않고 지금 믿음만 본다")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    if args.show:
        show(now)
        return

    reflector = build_reflector(load_settings())
    reflector.lookback = timedelta(days=args.days)
    result = asyncio.run(reflector.run_once(now))

    print(f"\n흔적 {result.considered}건 → 믿음 {len(result.learned)}건 갱신")
    if result.forgotten:
        print(f"잊음: {', '.join(result.forgotten)}")
    if result.skipped:
        # 조용히 넘기면 왜 아무것도 안 나왔는지 알 수 없다.
        print(f"기각 {len(result.skipped)}건:")
        for reason in result.skipped:
            print(f"  {reason}")
    show(now)


if __name__ == "__main__":
    main()
