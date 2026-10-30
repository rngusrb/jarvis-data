"""크롬 방문 기록 → 자비스 흔적.

**맥에서 돈다.** 다른 수집기와 다른 점이 그거다 — 아이폰 단축어는 스스로
서버로 밀어넣지만, 브라우저 기록은 누가 읽어서 보내줘야 한다.

크롬은 기기 간 동기화를 하므로 아이폰에서 본 것도 맥 DB에 들어온다.
시크릿 모드는 애초에 기록에 안 남아서 따로 거를 필요가 없다.

    /usr/bin/python3 -m collectors.chrome_history --dry-run   # 뭐가 갈지 먼저 본다
    /usr/bin/python3 -m collectors.chrome_history             # 보낸다

한 시간마다 자동으로 돌리려면 `collectors/install.sh` 를 쓴다.

## 알아둘 함정 넷

**DB가 잠긴다.** 크롬이 켜져 있으면 SQLite 잠금이 걸린다. 그래서 파일을
복사한 뒤 복사본을 읽는다. 크롬을 끄라고 하는 건 수집기로서 실격이다.

**시간이 유닉스 시간이 아니다.** 크롬은 1601-01-01 기준 마이크로초를 쓴다
(WebKit/Windows FILETIME 계열). 그냥 변환하면 1601년 데이터가 쌓인다.

**다른 기기 방문은 늦게 들어온다.** 아이폰에서 본 페이지는 동기화를 거쳐 맥
DB에 들어오는데, 방문 시각은 원래 시각 그대로다. 2026-10 실측으로 2주간 1,953건 중
90%는 1.4시간 안에 들어왔지만 최대 12.8시간 늦은 것도 있었다. 그래서 "어느
시각까지 보냈나"가 아니라 **"몇 번째 줄까지 읽었나"(visits.id)** 를 기억한다.
id 는 AUTOINCREMENT 라 다시 쓰이지 않고, 늦게 들어온 줄도 새 번호를 받는다.

**겹쳐 읽어도 같은 흔적이 나와야 한다.** 접는 칸이 시계에 고정돼 있어야(:00,
:30) 어디서부터 읽든 같은 방문이 같은 칸에 떨어진다. 첫 방문 기준으로 30분을
재면 읽기 시작점에 따라 접히는 모양이 달라져, 이미 접어 보낸 방문이 새 줄로
다시 들어간다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from collectors.common import SendError, load_state, post, save_state, server, token

# 1601-01-01 부터 1970-01-01 까지의 초.
WEBKIT_EPOCH_OFFSET = 11_644_473_600

CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"


def find_profile(root: Path = CHROME_ROOT) -> Path:
    """실제로 쓰는 프로필의 History 경로.

    "Default"로 박아두면 안 된다. 크롬은 계정을 추가할 때마다 Profile 1,
    Profile 5 … 를 만들고, 정작 Default 는 몇 달 전에 멈춰 있는 일이 흔하다
    (이 맥은 프로필이 7개고 Default 는 8월 9일이 마지막이다).

    Local State 가 마지막으로 쓴 프로필을 적어둔다. 그게 없으면 History
    파일이 가장 최근에 바뀐 프로필로 떨어진다.
    """
    state = root / "Local State"
    if state.exists():
        try:
            last = json.loads(state.read_text()).get("profile", {}).get("last_used")
        except (json.JSONDecodeError, OSError):
            last = None
        if last and (root / str(last) / "History").exists():
            return root / str(last) / "History"

    found = sorted(root.glob("*/History"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not found:
        raise SystemExit(f"크롬 프로필을 찾지 못했다: {root}")
    return found[0]


def profile_label(history_path: Path, root: Path = CHROME_ROOT) -> str:
    """사람이 알아볼 이름. 어느 계정 기록인지 모르면 잘못 모아도 모른다."""
    name = history_path.parent.name
    state = root / "Local State"
    if state.exists():
        try:
            info = json.loads(state.read_text()).get("profile", {}).get("info_cache", {})
        except (json.JSONDecodeError, OSError):
            info = {}
        entry = info.get(name, {})
        who = entry.get("user_name") or entry.get("name")
        if who:
            return f"{name} ({who})"
    return name


# 검색어를 품고 있는 호스트와 그 쿼리 파라미터.
SEARCH_HOSTS = {
    "www.google.com": "q",
    "google.com": "q",
    "www.bing.com": "q",
    "search.naver.com": "query",
    "m.search.naver.com": "query",
    "duckduckgo.com": "q",
    "www.youtube.com": "search_query",
    "github.com": "q",
}

# 흔적으로서 가치가 없는 것들. 저장은 다 하자는 방침이지만 이건 데이터가
# 아니라 잡음이다 — 자기 서버 대시보드를 200번 새로고침한 기록 같은 것.
SKIP_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "100.98.90.38", "100.65.255.109"}


def webkit_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000 - WEBKIT_EPOCH_OFFSET, tz=timezone.utc)


def describe(url: str, title: str) -> Optional[str]:
    """사람이 읽는 한 줄로 바꾼다.

    검색이면 검색어가, 아니면 페이지 제목이 훨씬 쓸모 있다. URL 자체는
    프롬프트에서 자리만 차지하고 의미를 거의 안 준다.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host in SKIP_HOSTS:
        return None

    param = SEARCH_HOSTS.get(host)
    if param:
        values = parse_qs(parsed.query).get(param)
        if values and values[0].strip():
            return f"검색: {unquote(values[0]).strip()}"
        # 검색어 없는 구글 방문(그냥 홈)은 흔적이 아니다.
        return None

    clean = title.strip()
    if not clean:
        return None
    return f"{clean} ({host})" if host else clean


SOURCE = "mac_chrome"
KIND = "web_visit"
STATE_NAME = "chrome_history"

# 같은 칸 안에서 같은 페이지를 다시 열면 새 흔적이 아니라 같은 행동으로 본다.
# 새로고침·뒤로가기가 흔해서 접지 않으면 한 페이지가 프롬프트를 다섯 줄씩
# 먹는다. 몇 번 봤는지는 meta 에 남겨 관심의 강도를 잃지 않는다.
GRID = timedelta(minutes=30)

# 한 요청에 너무 많이 싣지 않는다. 며칠 끊겼다 몰아 보낼 때 수천 건이 된다.
CHUNK = 500


@dataclass(frozen=True)
class Visit:
    id: int
    at: datetime
    url: str
    title: str
    other_device: bool


def floor_grid(at: datetime) -> datetime:
    epoch = at.timestamp()
    return datetime.fromtimestamp(epoch - epoch % GRID.total_seconds(), tz=timezone.utc)


def to_webkit(at: datetime) -> int:
    return int((at.timestamp() + WEBKIT_EPOCH_OFFSET) * 1_000_000)


@contextmanager
def snapshot(db_path: Path) -> Iterator[sqlite3.Connection]:
    """잠금을 피하려고 복사본을 연다.

    `History-journal` 도 같이 복사한다. 크롬이 쓰는 도중에 복사하면 본 파일만으로는
    반쯤 바뀐 상태일 수 있는데, 저널이 옆에 있으면 SQLite 가 열면서 되돌린다.
    """
    if not db_path.exists():
        raise SystemExit(f"크롬 기록 파일이 없다: {db_path}")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "History"
        shutil.copy2(db_path, copy)
        for suffix in ("-journal", "-wal"):
            side = db_path.with_name(db_path.name + suffix)
            if side.exists():
                shutil.copy2(side, copy.with_name(copy.name + suffix))
        conn = sqlite3.connect(copy)
        try:
            yield conn
        finally:
            conn.close()


def _select(conn: sqlite3.Connection, where: str, params: Tuple[Any, ...]) -> List[Visit]:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(visits)")}
    # 다른 기기에서 온 방문 표시. 오래된 크롬에는 이 열이 없다.
    other = (
        "COALESCE(v.originator_cache_guid, '') != ''" if "originator_cache_guid" in columns else "0"
    )
    rows = conn.execute(
        f"SELECT v.id, v.visit_time, u.url, u.title, {other} "
        f"FROM visits v JOIN urls u ON u.id = v.url WHERE {where} ORDER BY v.id",
        params,
    ).fetchall()
    return [
        Visit(int(i), webkit_to_datetime(int(t)), str(url), str(title or ""), bool(o))
        for i, t, url, title, o in rows
    ]


def fold(visits: Iterable[Visit]) -> List[Dict[str, Any]]:
    """같은 칸 안의 같은 페이지는 하나로 접는다. 시각은 **처음 본 때**를 남긴다."""
    groups: Dict[Tuple[datetime, str], List[Visit]] = {}
    for visit in sorted(visits, key=lambda v: (v.at, v.id)):
        text = describe(visit.url, visit.title)
        if text is None:
            continue
        groups.setdefault((floor_grid(visit.at), text), []).append(visit)

    traces: List[Dict[str, Any]] = []
    for (_, text), group in groups.items():
        first = group[0]
        others = sum(v.other_device for v in group)
        meta: Dict[str, Any] = {
            "url": first.url,
            "host": urlparse(first.url).hostname or "",
            # 폰으로 본 것과 맥으로 본 것은 다른 시간을 보낸 것이다.
            "device": "맥" if not others else "다른 기기" if others == len(group) else "둘 다",
        }
        if len(group) > 1:
            meta["visits"] = len(group)
        traces.append({"at": first.at.isoformat(), "text": text, "meta": meta})
    return sorted(traces, key=lambda t: str(t["at"]))


def plan(
    conn: sqlite3.Connection, after_id: Optional[int], since: datetime, now: datetime
) -> Tuple[List[Dict[str, Any]], Optional[int], int]:
    """보낼 흔적, 보내고 나면 옮길 커서, 다음으로 미룬 방문 수.

    지금 칸은 아직 안 닫혔다. 지금 보내면 방문 수가 덜 세진 채로 들어가고,
    나중에 다 센 줄은 (시각, 텍스트)가 같아 서버가 무시한다. 그래서 닫힌 칸만
    보내고, 커서는 미룬 방문 앞에서 멈춘다.

    새 방문이 닿은 칸은 그 칸의 **모든** 방문으로 다시 접는다. 새로 읽은 줄만
    접으면 같은 칸이 읽을 때마다 다르게 접힌다.
    """
    if after_id is None:
        # 처음 돌 때만 시각으로 자른다. 그 뒤로는 id 가 기준이다.
        new = _select(conn, "v.visit_time >= ?", (to_webkit(since),))
    else:
        new = _select(conn, "v.id > ?", (after_id,))
    if not new:
        return [], after_id, 0

    open_from = floor_grid(now)
    waiting = [v for v in new if v.at >= open_from]
    cursor = min(v.id for v in waiting) - 1 if waiting else max(v.id for v in new)

    visits: List[Visit] = []
    for start in sorted({floor_grid(v.at) for v in new if v.at < open_from}):
        visits += _select(
            conn,
            "v.visit_time >= ? AND v.visit_time < ?",
            (to_webkit(start), to_webkit(start + GRID)),
        )
    return fold(visits), cursor, len(waiting)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="크롬 방문 기록을 자비스로 보낸다")
    parser.add_argument(
        "--hours", type=float, default=24.0, help="처음 돌 때(커서가 없을 때) 몇 시간치를 읽을지"
    )
    parser.add_argument("--reset", action="store_true", help="커서를 잊고 --hours 부터 다시")
    parser.add_argument(
        "--profile", type=Path, default=None, help="History 파일 경로. 생략하면 자동 감지"
    )
    parser.add_argument("--server", default=server())
    parser.add_argument("--dry-run", action="store_true", help="보내지 않고 무엇이 갈지만 본다")
    args = parser.parse_args(argv)

    history = args.profile or find_profile()
    # 프로필마다 id 가 따로 매겨진다. 프로필을 바꾸면 커서도 따로 간다.
    key = str(history)
    state = load_state(STATE_NAME)
    after_id = None if args.reset else state.get(key, {}).get("after_id")
    now = datetime.now(timezone.utc)

    with snapshot(history) as conn:
        traces, cursor, waiting = plan(conn, after_id, now - timedelta(hours=args.hours), now)
    print(
        f"{now:%m-%d %H:%M} 프로필 {profile_label(history)} · 커서 {after_id} → {cursor} · "
        f"흔적 {len(traces)}개 · 안 닫힌 칸이라 미룬 방문 {waiting}건"
    )

    if args.dry_run:
        for item in traces[-40:]:
            print(f"  {item['at'][:16]}  {item['meta']['device']:<5} {item['text'][:80]}")
        return 0

    auth = token()
    if not auth:
        print("토큰이 없다 — JARVIS_INGEST_TOKEN 또는 상태 폴더의 token 파일", file=sys.stderr)
        return 1

    written = 0
    try:
        for i in range(0, len(traces), CHUNK):
            body = {"source": SOURCE, "kind": KIND, "traces": traces[i : i + CHUNK]}
            written += int(post(args.server, auth, "/ingest/traces", body).get("written", 0))
    except SendError as exc:
        # 커서를 옮기지 않는다. 다음 실행이 같은 자리부터 다시 보낸다 — 서버가
        # 중복을 무시하므로 앞 덩어리가 이미 들어갔어도 안전하다.
        print(f"보내지 못했다, 커서는 그대로 둔다: {exc}", file=sys.stderr)
        return 1

    if cursor is not None:
        state[key] = {"after_id": cursor, "sent_at": now.isoformat()}
        save_state(STATE_NAME, state)
    # 받은 수와 새로 저장된 수가 다른 게 정상이다. 칸을 다시 접어 보내면
    # 이미 있는 줄은 서버가 무시한다.
    print(f"보냄 {len(traces)}개 → 새로 저장 {written}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
