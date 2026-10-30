"""파일에 적힌 줄 → 자비스 서버. 폰과 맥 수집기의 공통 배달부다.

두 곳을 읽는다.

- 아이폰 단축어가 iCloud Drive 에 덧붙이는 파일: `단축어/jarvis/*.jsonl`
- 맥 수집기들(`chrome_history`, `mac_apps`)이 적는 보낼 상자: `상태 폴더/outbox/*.jsonl`

    /usr/bin/python3 -m collectors.forward --dry-run   # 무엇이 갈지 본다
    /usr/bin/python3 -m collectors.forward             # 보낸다 (launchd 가 5분마다)
    /usr/bin/python3 -m collectors.forward --replay    # 거절됐던 줄을 다시 보낼 상자로

## 왜 단축어가 서버에 직접 쏘지 않나

직접 쏘면 **그 순간** 폰이 서버에 닿아야 한다. 2026-08-25 부터 52일 중 폰 데이터가
들어온 날은 9일이었다. 폰 Tailscale 이 꺼져 있었고, 실패한 단축어의 데이터는 다시
보낼 곳 없이 사라졌다. 파일에 적으면 iCloud 가 인터넷만 되면 맥으로 옮겨 오고,
맥이 서버에 닿을 때 보낸다. 어디서 끊겨도 기다릴 뿐 사라지지 않는다.

## 한 줄의 모양

서버로 갈 요청 본문 그대로에 **어느 문으로 갈지(`to`)** 하나를 더한다. 단축어가
지금 만드는 텍스트 앞에 `"to":"…",` 만 붙이면 된다.

    {"to":"samples","kind":"location","samples":[...]}
    {"to":"spans","spans":[...]}
    {"to":"traces","source":"mac_apps","kind":"mac_app","traces":[...]}

파일은 덧붙이기만 한다. 어디까지 보냈는지(바이트 위치)를 파일마다 기억한다.
줄바꿈이 빠지거나 객체가 여러 줄에 걸쳐도 읽는다 — 단축어가 무엇을 내놓을지
폰을 열어보기 전엔 확신할 수 없다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from collectors.common import (
    Rejected,
    Retry,
    load_state,
    post,
    save_state,
    server,
    set_aside,
    state_dir,
    token,
)

STATE_NAME = "forward"

ROUTES = {
    "observations": "/ingest",
    "spans": "/ingest/spans",
    "samples": "/ingest/samples",
    "traces": "/ingest/traces",
}

INBOX = Path.home() / "Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/jarvis"

# 파일 앞부분의 지문. 같은 이름으로 **다른** 파일이 생기면(지우고 새로 만들면)
# 바이트 위치가 엉뚱한 데를 가리킨다. 덧붙이기만 했다면 앞부분은 안 바뀐다.
HEAD = 256

# 끝의 못 읽는 줄을 "덜 쓴 것"으로 보고 기다리는 기한.
STALE_TAIL = timedelta(days=1)

# 깨진 줄을 건너뛸 때 다음 객체가 시작하는 곳. 줄바꿈 뒤의 `{`, 또는 줄바꿈이
# 빠졌어도 줄의 첫 열쇠인 `{"to":`.
NEXT_OBJECT = re.compile(r'\n\s*\{|\{\s*"to"\s*:')

Send = Callable[[str, Dict[str, Any]], Dict[str, Any]]


@dataclass
class Report:
    sent: int = 0
    rejected: int = 0
    # 서버에 못 닿아 멈췄으면 그 이유. 남은 줄은 다음 실행이 보낸다.
    stopped: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def inbox_dir() -> Path:
    return Path(os.getenv("JARVIS_INBOX") or INBOX)


@dataclass
class Found:
    paths: List[Path]
    notes: List[str]
    # 폴더를 못 열었다. 나머지는 보내되 실패로 끝낸다.
    failed: bool = False


def files(create: bool = True) -> Found:
    """보낼 파일들. 폴더 하나를 못 열어도 다른 폴더는 본다.

    iCloud 폴더는 launchd 로 띄운 파이썬에게 권한(TCC)이 막힐 수 있다. 그렇다고
    맥 상자까지 못 보내면 안 된다.
    """
    found = Found([], [])
    inbox = inbox_dir()
    for folder in (inbox, state_dir() / "outbox"):
        try:
            if folder == inbox and not folder.is_dir():
                if not folder.parent.is_dir():
                    # iCloud Drive 를 안 쓰는 맥이다. 5분마다 찍으면 진짜 문제가 묻히므로
                    # 시험 실행에서만 알린다. 폰 데이터가 끊기면 서버의 수집 감시가 잡는다.
                    if not create:
                        found.notes.append(f"iCloud 단축어 폴더가 없다: {folder.parent}")
                elif create:
                    # 만들어 두면 폰의 파일 앱에서도 보여 단축어가 그 경로를 쓴다.
                    folder.mkdir()
                    found.notes.append(f"받은 편지함을 만들었다: {folder}")
                else:
                    found.notes.append(f"받은 편지함이 아직 없다 (보내기 실행 때 만든다): {folder}")
                continue
            if folder.is_dir():
                found.paths += sorted(p for p in folder.iterdir() if p.suffix in (".jsonl", ".txt"))
        except OSError as exc:
            found.notes.append(f"{folder} 를 못 열었다: {exc}")
            found.failed = True
    return found


def parse(text: str) -> Iterator[Tuple[Optional[Any], str, int]]:
    """(읽은 값 또는 None, 원문, 끝 위치). 끝에 덜 쓴 줄이 있으면 거기서 멈춘다.

    깨진 줄이 중간에 있으면 None 으로 내놓고 다음 객체로 넘어간다. 맨 끝의 깨진
    줄은 **덜 쓴 것**과 구별할 수 없어 기다린다 — 다음 줄이 붙으면 그때 넘어간다.
    """
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return
        try:
            value, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            following = NEXT_OBJECT.search(text, pos + 1)
            if following is None:
                return
            yield None, text[pos : following.start()], following.start()
            pos = following.start()
            continue
        # 뒤따르는 줄바꿈까지 읽은 것으로 친다. 다 보냈으면 커서가 파일 끝에 선다.
        after = end
        while after < len(text) and text[after].isspace():
            after += 1
        yield value, text[pos:end], after
        pos = after


def _head(data: bytes, upto: int) -> str:
    return hashlib.sha1(data[: min(upto, HEAD)]).hexdigest()


def _deliver(
    path: Path, text: str, send: Send, report: Report, rejects: List[Dict[str, Any]]
) -> int:
    """text 안의 온전한 객체를 보내고, 처리한 데까지의 바이트 수를 돌려준다."""
    done_chars, done_bytes = 0, 0
    for value, raw, end in parse(text):
        try:
            if not isinstance(value, dict):
                raise Rejected("JSON 객체로 읽히지 않는다")
            route = ROUTES.get(str(value.get("to")))
            if route is None:
                raise Rejected(f"'to' 가 {sorted(ROUTES)} 중 하나여야 한다: {value.get('to')!r}")
            send(route, {k: v for k, v in value.items() if k != "to"})
            report.sent += 1
        except Rejected as exc:
            report.rejected += 1
            rejects.append({"file": str(path), "reason": str(exc), "raw": raw})
        except Retry as exc:
            report.stopped = str(exc)
            break
        done_bytes += len(text[done_chars:end].encode("utf-8"))
        done_chars = end
    return done_bytes


def forward_file(
    path: Path, saved: Dict[str, Any], send: Send, report: Report, rejects: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """한 파일을 보낸 데까지 보내고, 새 커서를 돌려준다.

    서버에 못 닿으면 보낸 데까지의 커서를 돌려주고 report.stopped 를 채운다.
    """
    data = path.read_bytes()
    pos = int(saved.get("offset", 0))
    if pos > len(data) or saved.get("head") not in (None, _head(data, pos)):
        report.notes.append(f"{path.name} 이 바뀌었다 — 처음부터 다시 읽는다 (서버가 중복을 무시)")
        pos = 0

    while pos < len(data) and report.stopped is None:
        chunk = data[pos:]
        try:
            text, bad = chunk.decode("utf-8"), -1
        except UnicodeDecodeError as exc:
            text, bad = chunk[: exc.start].decode("utf-8"), pos + exc.start
        pos += _deliver(path, text, send, report, rejects)
        if bad < 0 or report.stopped is not None:
            break
        line_end = data.find(b"\n", bad)
        if line_end < 0:
            # 마지막 줄이다. 반쯤 쓰인 글자일 수 있어 다음 줄이 붙을 때까지 기다린다.
            break
        # UTF-8 이 아닌 바이트가 든 줄은 통째로 따로 두고 이어간다. 그 앞의 줄은 이미 보냈다.
        report.rejected += 1
        rejects.append(
            {
                "file": str(path),
                "reason": "UTF-8 이 아닌 바이트",
                "raw": data[pos : line_end + 1].decode("utf-8", errors="replace"),
            }
        )
        pos = line_end + 1

    leftover = data[pos:].strip()
    if leftover and report.stopped is None:
        if time.time() - path.stat().st_mtime > STALE_TAIL.total_seconds():
            # 덜 쓴 줄이었다면 그사이 이어졌을 것이다. 영원히 기다리면 5분마다 같은 안내만
            # 쌓인다 — 달이 바뀌어 다음 줄이 새 파일로 간 상자가 그렇다(리뷰 재현).
            report.rejected += 1
            rejects.append(
                {
                    "file": str(path),
                    "reason": "끝에 남은 깨진 줄 — 하루 넘게 이어지지 않았다",
                    "raw": data[pos:].decode("utf-8", errors="replace"),
                }
            )
            pos = len(data)
        else:
            waiting = f"{path.name} 끝에 아직 못 읽은 {len(leftover)}바이트"
            report.notes.append(f"{waiting} — 덜 쓴 줄이면 다음에 들어간다")
    return {"offset": pos, "head": _head(data, pos)}


def forward(
    paths: List[Path], state: Dict[str, Any], send: Send
) -> Tuple[Report, Dict[str, Any], List[Dict[str, Any]]]:
    """보낸 결과, 새 커서들, 거절된 줄. 아무것도 쓰지 않는다 — 시험 실행이 같은 길을 탄다."""
    report = Report()
    rejects: List[Dict[str, Any]] = []
    for path in paths:
        try:
            state[str(path)] = forward_file(path, state.get(str(path), {}), send, report, rejects)
        except OSError as exc:
            # iCloud 폴더는 권한(TCC)에 막힐 수 있다. 다른 파일은 계속 보낸다.
            report.notes.append(f"{path} 를 못 읽었다: {exc}")
        if report.stopped:
            break
    return report, state, rejects


def keep_rejected(rejects: List[Dict[str, Any]]) -> None:
    """거절된 줄은 버리지 않는다. 서버가 그 종류를 아직 몰랐던 거라면 되살려야 한다."""
    for item in rejects:
        set_aside(item["file"], item["reason"], item["raw"])


def replay() -> int:
    """거절됐던 줄을 보낼 상자로 되돌린다.

    서버가 새 종류를 모르는 채로 수집기가 먼저 돌면 그 줄들이 거절된다. 서버를
    고친 뒤 이걸 돌리면 다음 배달 때 다시 간다.
    """
    rejected = state_dir() / "rejected.jsonl"
    if not rejected.exists():
        print("거절된 줄 없음")
        return 0
    lines = [json.loads(line)["raw"] for line in rejected.read_text().splitlines() if line]
    target = state_dir() / "outbox" / f"replay-{datetime.now():%Y%m%d%H%M%S}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(raw.strip() + "\n" for raw in lines))
    rejected.unlink()
    print(f"{len(lines)}줄을 {target.name} 로 옮겼다 — 다음 배달 때 간다")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="파일에 적힌 줄을 자비스로 보낸다")
    parser.add_argument("--server", default=server())
    parser.add_argument("--dry-run", action="store_true", help="보내지 않고 무엇이 갈지만 본다")
    parser.add_argument("--replay", action="store_true", help="거절됐던 줄을 다시 보낼 상자로")
    args = parser.parse_args(argv)

    if args.replay:
        return replay()

    found = files(create=not args.dry_run)

    if args.dry_run:

        def show(route: str, body: Dict[str, Any]) -> Dict[str, Any]:
            print(f"  → {route}  {json.dumps(body, ensure_ascii=False)[:100]}")
            return {}

        report, _, _ = forward(found.paths, load_state(STATE_NAME), show)
        print(f"보낼 줄 {report.sent} · 거절될 줄 {report.rejected}")
        for note in found.notes + report.notes:
            print(f"  {note}")
        return 0

    auth = token()
    if not auth:
        print("토큰이 없다 — JARVIS_INGEST_TOKEN 또는 상태 폴더의 token 파일", file=sys.stderr)
        return 1

    def send(route: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return post(args.server, auth, route, body)

    report, state, rejects = forward(found.paths, load_state(STATE_NAME), send)
    keep_rejected(rejects)
    save_state(STATE_NAME, state)
    report.notes = found.notes + report.notes

    # 할 일이 없었으면 아무것도 안 찍는다. 5분마다 "0건" 이 쌓이면 진짜 문제가 묻힌다.
    if report.sent or report.rejected or report.stopped or report.notes:
        stamp = f"{datetime.now():%m-%d %H:%M}"
        print(f"{stamp} 보냄 {report.sent}줄 · 거절 {report.rejected}줄", flush=True)
        for note in report.notes:
            print(f"  {note}", flush=True)
        if report.rejected:
            print(f"  거절된 줄은 {state_dir() / 'rejected.jsonl'} 에 있다", flush=True)
        if report.stopped:
            print(f"  멈춤, 남은 줄은 다음에: {report.stopped}", file=sys.stderr, flush=True)
            return 1
    return 1 if found.failed else 0


if __name__ == "__main__":
    sys.exit(main())
