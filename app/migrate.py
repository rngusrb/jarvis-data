"""관측치로 담겨 있던 사건 종류를 사건 표로 옮긴다. 한 번이면 되고, 다시 돌려도 안전하다.

    python -m app.migrate            # 무엇을 옮길지 본다
    python -m app.migrate --apply    # 옮긴다 (DB 를 먼저 백업한다)

2026-11 위치를 관측치에서 사건으로 옮기면서 생겼다. 서버가 새 코드로 뜨면 위치는 사건
표로 들어가는데, 옛 위치가 관측치 표에 남아 있으면 같은 이름이 두 표에 생겨 "마지막
수신"이 어느 쪽인지 모른다 — 수집 감시가 멈추고 로그에 남는다. 배포하고 바로 돌린다.

옮긴 줄을 사건 표에서 다시 읽어 전부 있는지 확인한 **뒤에** 관측치를 지운다. 섹터를 아는
파일은 `app/main.py` 하나라 여기서는 그 목록을 가져다 쓴다.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import List, Optional

from app.main import SECTORS
from src.core.clock import stamp
from src.core.config import load_settings
from src.core.events import event_from_point
from src.storage.events import SQLiteEventStore
from src.storage.sqlite import SQLiteStore

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="관측치로 담긴 사건을 사건 표로 옮긴다")
    parser.add_argument("--apply", action="store_true", help="실제로 옮긴다")
    args = parser.parse_args(argv)

    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    events = SQLiteEventStore(settings.db_path)

    for card in (card for sector in SECTORS for card in sector.EVENTS):
        rows = store.recent(card.kind, EPOCH)
        try:
            moved = [
                event_from_point(o.source, card.kind, o.at, o.value, o.meta, card.value_as)
                for o in rows
            ]
        except ValueError as exc:
            print(f"  옮길 수 없는 줄이 있다 — 아무것도 바꾸지 않는다: {exc}", file=sys.stderr)
            return 1
        labels = sorted({e.label or "(없음)" for e in moved})
        print(f"{card.label}({card.kind}): 관측치 {len(rows)}건 → 사건  이유 {labels}")
        if not args.apply or not rows:
            continue

        written = events.write(moved)
        stored = {(e.source, stamp(e.at), e.label) for e in events.recent(card.kind, EPOCH)}
        missing = [e for e in moved if (e.source, stamp(e.at), e.label) not in stored]
        if missing:
            print(f"  사건 표에 {len(missing)}건이 없다 — 관측치를 지우지 않는다", file=sys.stderr)
            return 1
        removed = store.remove(rows)
        already = len(moved) - written
        print(f"  사건 새로 {written}건 (이미 있던 것 {already}건), 관측치 {removed}건 지움")

    if not args.apply:
        print("\n시험 실행이다. 옮기려면 --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
