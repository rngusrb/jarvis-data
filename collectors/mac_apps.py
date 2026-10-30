"""맥에서 앞에 떠 있는 앱 → 자비스 흔적.

**계속 떠 있는 수집기다.** 30초마다 앞 앱과 마지막 입력 이후 흐른 시간을 보고,
같은 앱이 이어진 구간을 하나로 묶는다. 구간이 끝나면 보낼 상자(outbox)에 한 줄
적고, 실제로 보내는 건 `collectors.forward` 가 한다. 서버가 꺼져 있어도
상자에 쌓여 있다가 나중에 들어간다.

    /usr/bin/python3 -m collectors.mac_apps --once   # 지금 무엇이 잡히는지 본다
    /usr/bin/python3 -m collectors.mac_apps          # 계속 돈다 (launchd 가 띄운다)

권한이 필요 없다. `lsappinfo` 와 `ioreg` 는 손쉬운 사용·화면 기록 권한 없이 돈다.
창 제목은 읽지 않는다 — 그건 권한이 필요하고, 앱 이름만으로도 리듬은 보인다.

## 구간이 끝나는 때

- 다른 앱이 앞으로 왔다 → 바뀐 시각에 끝
- 5분 넘게 입력이 없다 → **마지막 입력 시각**에 끝. 자리를 비운 시간을 앱에 붙이지 않는다
- 표본 사이가 90초 넘게 벌어졌다(맥이 잠들었다) → 마지막 표본 시각에 끝
- 정시를 넘었다 → 정시에서 자르고 이어서 연다. 시간대별 합계가 정확해지고,
  수집기가 죽어도 잃는 건 한 시간 안쪽이다
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from collectors.common import load_state, save_state, state_dir

SOURCE = "mac_apps"
KIND = "mac_app"
STATE_NAME = "mac_apps"

INTERVAL = timedelta(seconds=30)
IDLE_AFTER = timedelta(minutes=5)
# 표본이 이보다 벌어지면 그 사이엔 맥이 잠들어 있었다.
GAP = INTERVAL * 3
# 이보다 짧은 구간은 앱 전환 중에 스친 것이다.
SHORTEST = timedelta(seconds=5)

# 앞에 떠 있어도 사람이 쓰는 앱이 아닌 것. 잠금 화면이 대표적이다.
NOT_USE = {"loginwindow", "ScreenSaverEngine"}


@dataclass
class Span:
    app: str
    start: datetime
    end: datetime

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


def _floor_hour(at: datetime) -> datetime:
    return at.replace(minute=0, second=0, microsecond=0)


class Tracker:
    """표본을 받아 끝난 구간을 내놓는다. 시계와 맥을 모르게 짜서 테스트가 된다."""

    def __init__(self, current: Optional[Span] = None, last: Optional[datetime] = None) -> None:
        self.current = current
        self.last = last

    def observe(self, at: datetime, app: Optional[str], idle: timedelta) -> List[Span]:
        done: List[Span] = []
        if self.current and self.last and at - self.last > GAP:
            done.append(self.current)
            self.current = None

        using = app if app and app not in NOT_USE and idle < IDLE_AFTER else None
        if self.current and using != self.current.app:
            if using:
                self.current.end = at
            else:
                self.current.end = max(self.current.start, at - idle)
            done.append(self.current)
            self.current = None

        if using:
            if self.current is None:
                self.current = Span(using, at, at)
            else:
                self.current.end = at
            hour = _floor_hour(at)
            if self.current.start < hour:
                done.append(Span(using, self.current.start, hour))
                self.current = Span(using, hour, at)

        self.last = at
        return [piece for span in done for piece in _split_hours(span) if _long_enough(piece)]

    def dump(self) -> Dict[str, Any]:
        current = None
        if self.current:
            current = {
                "app": self.current.app,
                "start": self.current.start.isoformat(),
                "end": self.current.end.isoformat(),
            }
        return {"current": current, "last": self.last.isoformat() if self.last else None}

    @classmethod
    def restore(cls, data: Dict[str, Any]) -> "Tracker":
        """수집기가 다시 떠도 하던 구간을 잇는다. 오래 쉬었으면 첫 표본이 닫는다."""
        raw = data.get("current")
        current = None
        if raw:
            current = Span(
                str(raw["app"]),
                datetime.fromisoformat(raw["start"]),
                datetime.fromisoformat(raw["end"]),
            )
        last = datetime.fromisoformat(data["last"]) if data.get("last") else None
        return cls(current, last)


def _split_hours(span: Span) -> List[Span]:
    pieces: List[Span] = []
    start = span.start
    while _floor_hour(start) + timedelta(hours=1) < span.end:
        cut = _floor_hour(start) + timedelta(hours=1)
        pieces.append(Span(span.app, start, cut))
        start = cut
    pieces.append(Span(span.app, start, span.end))
    return pieces


def _long_enough(span: Span) -> bool:
    return span.end - span.start >= SHORTEST


def describe(span: Span) -> str:
    seconds = span.seconds
    if seconds < 60:
        return f"{span.app} {seconds}초"
    return f"{span.app} {round(seconds / 60)}분"


def to_line(spans: List[Span]) -> str:
    """보낼 상자에 적을 한 줄. 폰 단축어가 쓰는 줄과 같은 모양이다."""
    traces = [
        {
            "at": s.start.isoformat(),
            "text": describe(s),
            "meta": {"app": s.app, "end": s.end.isoformat(), "seconds": s.seconds},
        }
        for s in spans
    ]
    body = {"to": "traces", "source": SOURCE, "kind": KIND, "traces": traces}
    return json.dumps(body, ensure_ascii=False)


def outbox(at: datetime) -> Path:
    """한 달에 한 파일. 끝없이 자라는 파일 하나보다 지우기 쉽다."""
    path = state_dir() / "outbox" / f"{SOURCE}-{at:%Y-%m}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def front_app() -> Optional[str]:
    """앞에 떠 있는 앱 이름. 못 읽으면 None — 잠금 화면이나 로그인 창일 때 그렇다."""
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=5)
    front = asn.stdout.strip()
    if not front:
        return None
    info = subprocess.run(
        ["lsappinfo", "info", "-only", "name", front], capture_output=True, text=True, timeout=5
    )
    found = re.search(r'"LSDisplayName"="([^"]*)"', info.stdout)
    return found.group(1) if found else None


def idle_time() -> timedelta:
    """마지막 키보드·마우스 입력 이후 흐른 시간."""
    out = subprocess.run(
        ["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True, timeout=5
    ).stdout
    found = re.search(r'"HIDIdleTime" = (\d+)', out)
    if not found:
        # 못 읽었다고 "쓰는 중"으로 치면 자리를 비운 시간이 전부 앱에 붙는다.
        raise RuntimeError("HIDIdleTime 을 못 읽었다 — ioreg 출력이 바뀌었나")
    return timedelta(seconds=int(found.group(1)) / 1e9)


def run() -> None:
    tracker = Tracker.restore(load_state(STATE_NAME))
    print(f"{datetime.now():%m-%d %H:%M} 시작 — 이어받은 구간: {tracker.current}", flush=True)
    while True:
        at = datetime.now(timezone.utc)
        try:
            app, idle = front_app(), idle_time()
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            # 표본 하나 실패로 죽으면 launchd 가 10초마다 되살리며 로그만 쌓는다.
            # 건너뛰고, 길게 실패하면 다음 표본이 공백으로 보고 구간을 닫는다.
            print(f"{datetime.now():%m-%d %H:%M} 표본 실패: {exc}", file=sys.stderr, flush=True)
            time.sleep(INTERVAL.total_seconds())
            continue
        done = tracker.observe(at, app, idle)
        if done:
            # 상자에 먼저 쓰고 상태를 저장한다. 순서가 반대면 그 사이에 죽었을 때
            # 구간이 상태에서도 상자에서도 사라진다.
            with outbox(at).open("a") as f:
                f.write(to_line(done) + "\n")
        save_state(STATE_NAME, tracker.dump())
        time.sleep(INTERVAL.total_seconds())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="맥 앞 앱 사용 구간을 모은다")
    parser.add_argument("--once", action="store_true", help="표본 하나만 찍어 본다")
    args = parser.parse_args(argv)
    if args.once:
        print(f"앞 앱: {front_app()} · 입력 없이 {idle_time().total_seconds():.0f}초")
        print(f"보낼 상자: {outbox(datetime.now(timezone.utc))}")
        return 0
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
