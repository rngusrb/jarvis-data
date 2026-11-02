"""옛 규칙이 버린 검색 사이트의 보통 페이지를 되살린다 — 고친 날 한 번 돌리는 스크립트 (JAR-020).

2026-11-02 까지 `describe` 는 검색 사이트(SEARCH_HOSTS)에서 검색어가 없으면 방문을 버렸다. 유튜브
영상과 GitHub 저장소가 08-27 첫 수집기부터 흔적이 되지 못했다. 이 스크립트는 **옛 수집기가 처리한
번호**(커서 이하 + 커서 위에서 먼저 보낸 번호) 가운데 옛 규칙이 버리고 새 규칙은 남기는 방문만 골라,
수집기와 같은 `fold` 로 접어 보낼 상자에 적는다. 옛 규칙이 버린 방문은 보낸 적이 없으니 이미 보낸
방문을 다시 접는 일은 없다(가이드 금지사항). 커서 위는 새 수집기가 새 규칙으로 처리한다.

크롬은 기록을 약 90일 남긴다 — 늦게 돌릴수록 오래된 것부터 잃는다.

맥에서, 머지된 main 의 개발 저장소에서:

    launchctl bootout gui/$(id -u)/ai.jarvis.chrome    # 옛 수집기를 먼저 내린다 — 커서가 멈춘다
    /usr/bin/python3 -m collectors.revive_search_pages --dry-run
    /usr/bin/python3 -m collectors.revive_search_pages
    sh collectors/install.sh                            # 새 코드로 다시 올린다(바로 한 번 돈다)

옛 수집기가 올라가 있으면 돌지 않는다. 커서를 읽은 뒤에 옛 코드가 한 번 더 돌면, 그 사이 옛 규칙이
버린 방문이 아무 기록 없이 빠진다(재리뷰).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import parse_qs, urlparse

from collectors.chrome_history import (
    CHUNK,
    KIND,
    SEARCH_HOSTS,
    SOURCE,
    STATE_NAME,
    Visit,
    _select,
    describe,
    find_profile,
    fold,
    profile_label,
    snapshot,
)
from collectors.common import append_lines, load_state, outbox_file

JOB = "ai.jarvis.chrome"


def dropped_before(url: str) -> bool:
    """옛 규칙이 버렸나 — 검색 사이트인데 검색어가 없다."""
    parsed = urlparse(url)
    param = SEARCH_HOSTS.get(parsed.hostname or "")
    if not param:
        return False
    values = parse_qs(parsed.query).get(param)
    return not (values and values[0].strip())


def revive(conn: sqlite3.Connection, after_id: int, sent_above: Iterable[int]) -> List[Visit]:
    """옛 수집기가 처리한 방문 중 옛 규칙이 버리고 새 규칙은 남기는 것."""
    above = set(sent_above)
    processed = _select(conn, "v.id <= ?", (after_id,))
    processed += [v for v in _select(conn, "v.id > ?", (after_id,)) if v.id in above]
    return [v for v in processed if dropped_before(v.url) and describe(v.url, v.title)]


def chrome_loaded() -> bool:
    found = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{JOB}"], capture_output=True, check=False
    )
    return found.returncode == 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="옛 규칙이 버린 검색 사이트 페이지를 되살린다")
    parser.add_argument("--profile", type=Path, default=None, help="History 파일. 생략하면 자동")
    parser.add_argument("--dry-run", action="store_true", help="상자에 쓰지 않고 몇 건인지만 본다")
    args = parser.parse_args(argv)

    if chrome_loaded():
        print(
            "옛 크롬 수집기가 올라가 있다 — 먼저 내린다: "
            f"launchctl bootout gui/{os.getuid()}/{JOB}",
            file=sys.stderr,
        )
        return 1
    history = args.profile or find_profile()
    saved = load_state(STATE_NAME).get(str(history))
    if not saved or saved.get("after_id") is None:
        print(f"이 프로필의 커서가 상태에 없다: {history}", file=sys.stderr)
        return 1
    after_id = int(saved["after_id"])
    sent_above = [int(i) for i in saved.get("sent_above", [])]

    with snapshot(history) as conn:
        revived = revive(conn, after_id, sent_above)
    traces = fold(revived)
    hosts = Counter(urlparse(v.url).hostname for v in revived)
    print(
        f"프로필 {profile_label(history)} · 커서 {after_id}"
        f" · 커서 위에서 보낸 번호 {len(sent_above)}개"
        f" · 되살릴 방문 {len(revived)} → 흔적 {len(traces)}줄"
    )
    for host, n in hosts.most_common():
        print(f"  {host}: {n}")
    if not traces or args.dry_run:
        return 0

    lines = [
        json.dumps(
            {"to": "traces", "source": SOURCE, "kind": KIND, "traces": traces[i : i + CHUNK]},
            ensure_ascii=False,
        )
        for i in range(0, len(traces), CHUNK)
    ]
    append_lines(outbox_file(SOURCE, datetime.now(timezone.utc)), lines)
    print(f"보낼 상자에 {len(lines)}줄 적었다 — 배달부가 보낸다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
