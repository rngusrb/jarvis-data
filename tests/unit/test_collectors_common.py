"""수집기가 서버 실패를 어떻게 나누나 — 다시 시도할 것과 따로 둘 것.

수집기는 맥 시스템 파이썬(3.9)으로 돈다. 테스트는 3.10 이상에서 돌아서 3.9 에서만
다른 동작을 놓친다. 그래서 맥에 `/usr/bin/python3` 가 있으면 그걸로 한 번 더 돌린다.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator, Tuple

import pytest

from collectors.common import Rejected, Retry, post

ROOT = Path(__file__).resolve().parents[2]
SYSTEM_PYTHON = Path("/usr/bin/python3")


class Answer(BaseHTTPRequestHandler):
    """경로의 숫자를 상태 코드로 돌려준다. /200 이면 JSON 으로 답한다."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        code = int(self.path.strip("/"))
        body = json.dumps({"written": 1} if code == 200 else {"detail": "no"}).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = HTTPServer(("127.0.0.1", 0), Answer)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def silent() -> Iterator[Tuple[str, socket.socket]]:
    """연결은 받아놓고 아무 답도 안 하는 서버."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}", sock
    sock.close()


def test_받으면_답을_돌려준다(server: str) -> None:
    assert post(server, "t", "/200", {}) == {"written": 1}


@pytest.mark.parametrize("code", [400, 422])
def test_내용이_틀린_건_따로_둔다(server: str, code: int) -> None:
    with pytest.raises(Rejected):
        post(server, "t", f"/{code}", {})


@pytest.mark.parametrize("code", [401, 404, 500, 503])
def test_나머지는_나중에_다시_시도한다(server: str, code: int) -> None:
    # 401 은 토큰 문제, 404 는 서버가 옛 코드, 5xx 는 서버 사정. 줄은 멀쩡하다.
    with pytest.raises(Retry):
        post(server, "t", f"/{code}", {})


def test_서버가_없으면_다시_시도한다() -> None:
    with pytest.raises(Retry):
        post("http://127.0.0.1:9", "t", "/ingest", {}, timeout=2)


def test_답이_없으면_다시_시도한다(silent: Tuple[str, socket.socket]) -> None:
    with pytest.raises(Retry):
        post(silent[0], "t", "/ingest", {}, timeout=0.3)


def _system_python() -> str:
    if sys.platform != "darwin" or not SYSTEM_PYTHON.exists():
        pytest.skip("맥 시스템 파이썬이 없다 (CI)")
    return str(SYSTEM_PYTHON)


def test_수집기는_맥_시스템_파이썬에서_읽힌다() -> None:
    modules = (
        "collectors.common, collectors.chrome_history, collectors.mac_apps, collectors.forward"
    )
    result = subprocess.run(
        [_system_python(), "-c", f"import {modules}"], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_맥_시스템_파이썬에서도_답이_없으면_다시_시도한다(
    silent: Tuple[str, socket.socket],
) -> None:
    # 2026-10-30 리뷰: 3.9 에서는 socket.timeout 이 TimeoutError 가 아니라 빠져나갔다.
    script = (
        "from collectors.common import Retry, post\n"
        "try:\n"
        f"    post({silent[0]!r}, 't', '/ingest', {{}}, timeout=0.3)\n"
        "except Retry:\n"
        "    print('Retry')\n"
    )
    result = subprocess.run(
        [_system_python(), "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=20
    )
    assert result.stdout.strip() == "Retry", result.stderr
