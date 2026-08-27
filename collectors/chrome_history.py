"""크롬 방문 기록 → 자비스 흔적.

**맥에서 돈다.** 다른 수집기와 다른 점이 그거다 — 아이폰 단축어는 스스로
서버로 밀어넣지만, 브라우저 기록은 누가 읽어서 보내줘야 한다.

크롬은 기기 간 동기화를 하므로 아이폰에서 본 것도 맥 DB에 들어온다.
시크릿 모드는 애초에 기록에 안 남아서 따로 거를 필요가 없다.

    python collectors/chrome_history.py --hours 6

## 알아둘 함정 둘

**DB가 잠긴다.** 크롬이 켜져 있으면 SQLite 잠금이 걸린다. 그래서 파일을
복사한 뒤 복사본을 읽는다. 크롬을 끄라고 하는 건 수집기로서 실격이다.

**시간이 유닉스 시간이 아니다.** 크롬은 1601-01-01 기준 마이크로초를 쓴다
(WebKit/Windows FILETIME 계열). 그냥 변환하면 1601년 데이터가 쌓인다.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

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
        if last and (root / last / "History").exists():
            return root / last / "History"

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


# 같은 페이지를 이 시간 안에 다시 열면 새 흔적이 아니라 같은 행동으로 본다.
# 새로고침·뒤로가기가 흔해서 접지 않으면 한 페이지가 프롬프트를 다섯 줄씩
# 먹는다. 몇 번 봤는지는 meta 에 남겨 관심의 강도를 잃지 않는다.
COLLAPSE_WITHIN = timedelta(minutes=30)


def collapse(traces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """연달아 같은 텍스트면 하나로 접는다. 시각은 **처음 본 때**를 남긴다."""
    folded: List[Dict[str, Any]] = []
    last_at: Dict[str, datetime] = {}
    for item in traces:
        at = datetime.fromisoformat(item["at"])
        seen = last_at.get(item["text"])
        if seen is not None and at - seen < COLLAPSE_WITHIN:
            folded[-1]["meta"]["visits"] = folded[-1]["meta"].get("visits", 1) + 1
            continue
        last_at[item["text"]] = at
        folded.append(item)
    return folded


def read_history(db_path: Path, since: datetime) -> List[Dict[str, Any]]:
    """잠금을 피하려고 복사본을 읽는다."""
    if not db_path.exists():
        raise SystemExit(f"크롬 기록 파일이 없다: {db_path}")

    cutoff = int((since.timestamp() + WEBKIT_EPOCH_OFFSET) * 1_000_000)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "History"
        shutil.copy2(db_path, copy)
        conn = sqlite3.connect(copy)
        try:
            rows = conn.execute(
                "SELECT v.visit_time, u.url, u.title "
                "FROM visits v JOIN urls u ON u.id = v.url "
                "WHERE v.visit_time >= ? ORDER BY v.visit_time",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()

    found: List[Dict[str, Any]] = []
    for visit_time, url, title in rows:
        text = describe(str(url), str(title or ""))
        if text is None:
            continue
        found.append(
            {
                "at": webkit_to_datetime(int(visit_time)).isoformat(),
                "text": text,
                "meta": {"url": str(url), "host": urlparse(str(url)).hostname or ""},
            }
        )
    return collapse(found)


def send(server: str, token: str, traces: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload = json.dumps(
        {"source": "mac_chrome", "kind": "web_visit", "traces": traces}, ensure_ascii=False
    ).encode()
    request = urllib.request.Request(
        f"{server.rstrip('/')}/ingest/traces",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result: Dict[str, Any] = json.loads(response.read())
            return result
    except urllib.error.HTTPError as exc:
        # 조용히 실패하면 며칠 뒤에야 "왜 흔적이 안 쌓이지"를 묻게 된다.
        raise SystemExit(f"서버가 거절했다 ({exc.code}): {exc.read().decode()[:300]}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="크롬 방문 기록을 자비스로 보낸다")
    parser.add_argument("--hours", type=float, default=6.0, help="최근 몇 시간치를 읽을지")
    parser.add_argument(
        "--profile", type=Path, default=None, help="History 파일 경로. 생략하면 자동 감지"
    )
    parser.add_argument("--server", default=os.getenv("JARVIS_SERVER", "http://100.98.90.38:8100"))
    parser.add_argument("--token", default=os.getenv("JARVIS_INGEST_TOKEN", ""))
    parser.add_argument("--dry-run", action="store_true", help="보내지 않고 무엇이 갈지만 본다")
    args = parser.parse_args()

    history = args.profile or find_profile()
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    traces = read_history(history, since)
    print(f"프로필: {profile_label(history)}")

    if args.dry_run:
        for item in traces[-40:]:
            print(f"  {item['at'][:16]}  {item['text'][:88]}")
        print(f"\n{len(traces)}건 (최근 40건만 표시)")
        return

    if not args.token:
        raise SystemExit("JARVIS_INGEST_TOKEN 이 필요하다")
    if not traces:
        print("보낼 흔적 없음")
        return

    # 겹치는 구간을 다시 읽는 건 정상 동작이다. 서버가 중복을 무시하므로
    # received 와 written 이 다른 게 기본이다.
    result = send(args.server, args.token, traces)
    print(f"보냄 {result['received']}건 → 새로 저장 {result['written']}건")


if __name__ == "__main__":
    sys.exit(main())
