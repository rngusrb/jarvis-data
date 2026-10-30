"""크롬 방문 기록 → 자비스 흔적.

**맥에서 돈다.** 다른 수집기와 다른 점이 그거다 — 아이폰 단축어는 스스로
서버로 밀어넣지만, 브라우저 기록은 누가 읽어서 보내줘야 한다.

크롬은 기기 간 동기화를 하므로 아이폰에서 본 것도 맥 DB에 들어온다.
시크릿 모드는 애초에 기록에 안 남아서 따로 거를 필요가 없다.

    /usr/bin/python3 -m collectors.chrome_history --dry-run   # 뭐가 갈지 먼저 본다
    /usr/bin/python3 -m collectors.chrome_history             # 보낼 상자에 적는다

서버로 보내는 건 배달부(`collectors.forward`)가 한다 — 맥 앱 수집기와 같은 길이다.
한 시간마다 자동으로 돌리려면 `collectors/install.sh` 를 쓴다.

## 알아둘 함정 다섯

**DB가 잠긴다.** 크롬이 켜져 있으면 SQLite 잠금이 걸린다. 그래서 파일을
복사한 뒤 복사본을 읽는다. 크롬을 끄라고 하는 건 수집기로서 실격이다.

**시간이 유닉스 시간이 아니다.** 크롬은 1601-01-01 기준 마이크로초를 쓴다
(WebKit/Windows FILETIME 계열). 그냥 변환하면 1601년 데이터가 쌓인다.

**다른 기기 방문은 늦게 들어온다.** 아이폰에서 본 페이지는 동기화를 거쳐 맥
DB에 들어오는데, 방문 시각은 원래 시각 그대로다. 2026-10 실측으로 2주간 1,953건 중
90%는 1.4시간 안에 들어왔지만 최대 12.8시간 늦은 것도 있었다. 그래서 "어느
시각까지 보냈나"가 아니라 **"몇 번째 줄까지 읽었나"(visits.id)** 를 기억한다.
id 는 AUTOINCREMENT 라 다시 쓰이지 않고, 늦게 들어온 줄도 새 번호를 받는다.

**한 방문은 한 번만 보낸다.** 이미 보낸 방문을 다시 접어 보내면, 그사이 같은
칸에 더 이른 방문이 늦게 들어왔거나 페이지 제목이 바뀌었을 때 (시각, 텍스트)가
달라져 같은 방문이 새 줄로 또 들어간다. 서버의 중복 무시로는 못 막는다. 그래서
커서 아래는 전부 보낸 것이고, 커서 위에서 먼저 보낸 것은 번호로 따로 기억한다.

**크롬이 기록을 새로 만들 수 있다.** 모든 기간 기록 삭제나 손상 복구 뒤에는 번호가
1부터 다시 붙는다. 커서를 그대로 두면 새 방문이 전부 커서 아래로 숨어, 매시간
"흔적 0개"로 조용히 멈춘다. 번호의 최고 기록(sqlite_sequence)이 커서보다 작으면
처음부터 다시 읽는다.
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

from collectors.common import load_state, outbox_file, save_state

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


@dataclass
class Plan:
    traces: List[Dict[str, Any]]
    # 보내고 나면 저장할 커서. 이 번호까지는 전부 보냈다.
    after_id: Optional[int]
    # 커서 위인데 이미 보낸 방문 번호. 미룬 방문 때문에 커서가 멈춘 사이에 보낸 것들.
    sent_above: List[int]
    waiting: int
    # 크롬이 기록을 새로 만들어서 처음부터 읽었다.
    restarted: bool = False


def high_water(conn: sqlite3.Connection) -> int:
    """visits 에 지금까지 붙은 가장 큰 번호. 줄을 지워도 줄지 않는다(AUTOINCREMENT)."""
    try:
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'visits'").fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        row = conn.execute("SELECT MAX(id) FROM visits").fetchone()
    return int(row[0] or 0)


def plan(
    conn: sqlite3.Connection,
    after_id: Optional[int],
    sent_above: Iterable[int],
    since: datetime,
    now: datetime,
) -> Plan:
    """무엇을 보내고, 보낸 뒤 어디까지 보냈다고 적을지.

    지금 칸은 아직 안 닫혔다. 닫힐 때까지 미뤄야 한 칸의 방문이 한 번에 접힌다.
    커서는 미룬 방문 앞에서 멈추고, 그 위에서 이번에 보낸 것은 번호로 넘긴다.
    다음 실행은 커서 위를 다시 읽되 이미 보낸 번호는 건너뛴다.
    """
    restarted = False
    if after_id is not None and high_water(conn) < after_id:
        restarted, after_id, sent_above = True, 0, []
    if after_id is None:
        # 처음 돌 때만 시각으로 자른다. 그 뒤로는 id 가 기준이다.
        rows = _select(conn, "v.visit_time >= ?", (to_webkit(since),))
    else:
        rows = _select(conn, "v.id > ?", (after_id,))
    done = set(sent_above)
    new = [v for v in rows if v.id not in done]
    if not new:
        return Plan([], after_id, sorted(done), 0, restarted)

    open_from = floor_grid(now)
    waiting = [v for v in new if v.at >= open_from]
    closed = [v for v in new if v.at < open_from]
    cursor = min(v.id for v in waiting) - 1 if waiting else max(v.id for v in new)
    above = sorted(i for i in done | {v.id for v in closed} if i > cursor)
    return Plan(fold(closed), cursor, above, len(waiting), restarted)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="크롬 방문 기록을 자비스로 보낸다")
    parser.add_argument(
        "--hours", type=float, default=24.0, help="처음 돌 때(커서가 없을 때) 몇 시간치를 읽을지"
    )
    parser.add_argument("--reset", action="store_true", help="커서를 잊고 --hours 부터 다시")
    parser.add_argument(
        "--profile", type=Path, default=None, help="History 파일 경로. 생략하면 자동 감지"
    )
    parser.add_argument("--dry-run", action="store_true", help="보내지 않고 무엇이 갈지만 본다")
    args = parser.parse_args(argv)

    history = args.profile or find_profile()
    # 프로필마다 id 가 따로 매겨진다. 프로필을 바꾸면 커서도 따로 간다.
    key = str(history)
    state = load_state(STATE_NAME)
    saved = {} if args.reset else state.get(key, {})
    after_id = saved.get("after_id")
    now = datetime.now(timezone.utc)

    with snapshot(history) as conn:
        found = plan(
            conn, after_id, saved.get("sent_above", []), now - timedelta(hours=args.hours), now
        )
    if found.restarted:
        print(
            f"크롬 기록이 새로 만들어졌다 — 저장된 커서 {after_id} 가 지금 번호보다 크다. "
            "처음부터 다시 읽는다",
            file=sys.stderr,
        )
    traces = found.traces
    print(
        f"{datetime.now():%m-%d %H:%M} 프로필 {profile_label(history)} · "
        f"커서 {after_id} → {found.after_id} · "
        f"흔적 {len(traces)}개 · 안 닫힌 칸이라 미룬 방문 {found.waiting}건"
    )

    if args.dry_run:
        for item in traces[-40:]:
            print(f"  {item['at'][:16]}  {item['meta']['device']:<5} {item['text'][:80]}")
        return 0

    # 직접 보내지 않고 보낼 상자에 적는다. 직접 보내면 덩어리가 여럿인 실행이 중간에
    # 끊겼을 때 커서를 어디까지 옮길지 애매해, 다음 실행이 이미 들어간 방문을 다시 접어
    # 드물게 두 번 셌다(2026-10-30 리뷰 재현). 상자의 줄은 배달부가 줄 단위로 보내고
    # 줄 단위로 기억하므로, 끊긴 줄만 그대로 다시 간다 — 같은 줄이라 서버가 하나로 본다.
    lines = [
        json.dumps(
            {"to": "traces", "source": SOURCE, "kind": KIND, "traces": traces[i : i + CHUNK]},
            ensure_ascii=False,
        )
        for i in range(0, len(traces), CHUNK)
    ]
    if lines:
        with outbox_file(SOURCE, now).open("a") as f:
            f.write("".join(line + "\n" for line in lines))
    # 상자에 먼저 쓰고 커서를 저장한다. 그 사이에 죽으면 다음 실행이 같은 방문을 한 번 더
    # 적을 수 있지만, 순서가 반대면 방문이 상자에도 커서에도 없이 사라진다.
    if found.after_id is not None:
        state[key] = {
            "after_id": found.after_id,
            "sent_above": found.sent_above,
            "sent_at": now.isoformat(),
        }
        save_state(STATE_NAME, state)
    print(f"보낼 상자에 {len(traces)}개 ({len(lines)}줄) — 배달부가 보낸다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
