"""맥 수집기들이 함께 쓰는 것 — 상태 파일, 토큰, 서버로 보내기.

수집기는 **맥의 시스템 파이썬(3.9)** 으로 돈다. launchd 는 로그인 셸을 거치지
않아서 anaconda 같은 건 안 보인다. 그래서 표준 라이브러리만 쓰고 `src/` 를
가져오지 않는다 — 서버 코드는 3.11 문법과 pydantic 을 쓴다.

실행은 저장소 루트에서 모듈로 한다:

    cd ~/dev/jarvis-data && /usr/bin/python3 -m collectors.chrome_history --dry-run
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

DEFAULT_SERVER = "http://100.98.90.38:8100"


def state_dir() -> Path:
    """수집기들이 어디까지 보냈는지 적어두는 곳.

    저장소 안에 두지 않는다. 저장소를 지우거나 새로 받아도 커서가 살아 있어야
    같은 기록을 처음부터 다시 보내지 않는다.
    """
    path = Path(os.getenv("JARVIS_STATE_DIR") or Path.home() / "Library/Application Support/jarvis")
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_state(name: str) -> Dict[str, Any]:
    path = state_dir() / f"{name}.json"
    if not path.exists():
        return {}
    data: Dict[str, Any] = json.loads(path.read_text())
    return data


def save_state(name: str, data: Dict[str, Any]) -> None:
    """다 쓴 뒤에 바꿔 끼운다. 쓰다 죽으면 반쯤 쓴 커서가 남아 다음 실행이 깨진다."""
    path = state_dir() / f"{name}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def server() -> str:
    return os.getenv("JARVIS_SERVER") or DEFAULT_SERVER


def token() -> str:
    """환경변수가 먼저, 없으면 상태 폴더의 `token` 파일.

    launchd plist 에 토큰을 적으면 plist 를 보여주거나 저장소에 올릴 때 같이
    샌다. 파일 하나에 두고 권한을 600 으로 잠근다.
    """
    value = os.getenv("JARVIS_INGEST_TOKEN", "").strip()
    if value:
        return value
    path = state_dir() / "token"
    if path.exists():
        return path.read_text().strip()
    return ""


class SendError(Exception):
    """서버로 못 보냈다."""


class Retry(SendError):
    """지금은 안 되지만 나중엔 된다 — 서버가 꺼졌거나, 연결이 끊겼거나, 토큰이 틀렸다.

    보낼 것을 그대로 두고 멈춘다. 다음 실행이 같은 자리에서 다시 시도한다.
    """


class Rejected(SendError):
    """보낸 내용 자체가 틀렸다(400·422). 몇 번을 다시 보내도 같다.

    이걸 Retry 처럼 다루면 줄 하나가 뒤에 오는 모든 줄을 영원히 막는다.
    """


def post(
    server_url: str, auth: str, path: str, body: Dict[str, Any], timeout: float = 30
) -> Dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{server_url.rstrip('/')}{path}",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {auth}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code in (400, 422):
            raise Rejected(f"서버가 내용을 거절했다 ({exc.code}): {detail}") from exc
        if exc.code == 401:
            raise Retry(f"토큰이 틀렸다 (401): {detail}") from exc
        raise Retry(f"서버 오류 ({exc.code}): {detail}") from exc
    except (OSError, http.client.HTTPException) as exc:
        # 이름으로 고르지 않고 OSError 통째로 잡는다. 3.9 에서는 연결을 받아놓고 답이
        # 없을 때 나는 socket.timeout 이 TimeoutError 가 아니다(3.10 부터 같다).
        # 2026-10-30 리뷰에서 그게 빠져나가 배달부가 "파일을 못 읽었다"로 적는 걸 찾았다.
        raise Retry(f"서버에 닿지 않는다 ({server_url}): {exc!r}") from exc
    try:
        result: Dict[str, Any] = json.loads(raw)
    except ValueError as exc:
        raise Retry(f"서버 답이 JSON 이 아니다: {raw[:200]!r}") from exc
    return result


def set_aside(origin: str, reason: str, raw: str) -> None:
    """서버가 거절한 것을 버리지 않고 따로 둔다.

    서버가 그 종류를 아직 몰랐던 거라면(수집기를 서버보다 먼저 올렸다면) 고친 뒤
    되살려야 한다. `forward --replay` 가 raw 를 보낼 상자로 되돌리므로, raw 는
    배달부가 읽는 줄 모양(`to` 포함)이어야 한다.
    """
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "origin": origin,
        "reason": reason,
        "raw": raw,
    }
    with (state_dir() / "rejected.jsonl").open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
