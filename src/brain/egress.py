"""브라우저가 밖으로 나가는 문 — 공개 주소로만, 검사한 주소로만 붙는다(JAR-036).

진짜 브라우저는 무엇이든 연다: 이 서버 안의 서비스(Langfuse, Open WebUI, 자비스 자신 :8100),
Tailscale 너머의 맥, 공유기. 웹 글에 든 지시가 브라우저를 그쪽으로 보내면 그 내용이 말로 새어
나간다 — 페이지 읽기와 같은 위험이다(`web.py` 머리말). 그래서 크롬이 밖으로 나가는 길을 전부 이
문으로 모은다(크롬의 `--proxy-server`, 루프백도 빼지 않게 `<-loopback>`).

  - CONNECT(https·wss)와 일반 HTTP 모두 이름을 한 번 풀어 모든 주소가 공개망인지
    보고(`web.checked`), **검사한 주소로** 붙는다 — 검사와 접속 사이에 이름이 내부로 바뀌어도(DNS
    리바인딩) 따라가지 않는다.
  - 막은 것은 403 과 경고 로그. 세션마다 받는 양에 상한(`budget`) — 끝없이 흘리는 서버로 메모리를
    채우지 않게.
  - TLS 는 크롬과 사이트 사이에서 끝까지 간다. 문은 내용을 보지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Collection, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from src.brain.web import Blocked, Resolve, checked, resolve

logger = logging.getLogger(__name__)

Streams = Tuple[asyncio.StreamReader, asyncio.StreamWriter]
# 검사한 주소로 TCP 를 연다. 시험은 이것을 바꿔 끼워 공개 주소를 로컬 시험 서버로 잇는다.
Opener = Callable[[str, int], Awaitable[Streams]]

# 요청 머리(첫 줄 + 헤더)의 상한. 이보다 길면 받지 않는다.
HEAD_LIMIT = 16_384
# 세션 하나가 밖에서 받는 양의 상한. 페이지 수십 장이면 충분하다.
BUDGET = 60_000_000
# 이만큼 아무것도 안 오가면 연결을 닫는다.
IDLE = 30.0
CONNECT_TIMEOUT = 10.0
# 브라우저가 나갈 수 있는 포트. 공유기 관리 화면(8080) 같은 것이 공인 주소로 열렸다(JAR-036 리뷰) —
# 이 집의 공인 주소는 `web.checked` 가 막고, 그 밖의 집·회사 장비가 쓰는 포트도 닫아 둔다.
PORTS = (80, 443)
# 평문 HTTP 요청 본문의 상한(검색 폼 정도). 길이를 모르는 본문(chunked)은 받지 않는다.
BODY_LIMIT = 1_000_000
# 다음 홉으로 넘기지 않는 헤더(프록시 쪽 것).
_HOP = {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "upgrade"}


async def _open(host: str, port: int) -> Streams:
    return await asyncio.open_connection(host, port)


class EgressProxy:
    """127.0.0.1 에만 뜨는 프록시. `start()` 가 크롬에 넘길 주소를 돌려준다."""

    def __init__(
        self,
        lookup: Resolve = resolve,
        budget: int = BUDGET,
        idle: float = IDLE,
        opener: Opener = _open,
        ports: Collection[int] = PORTS,
    ) -> None:
        self.lookup = lookup
        self.budget = budget
        self.idle = idle
        self.opener = opener
        self.ports = set(ports)
        # 밖에서 받은 바이트(세션 전체). 상한을 넘으면 새 연결도 거절한다.
        self.received = 0
        # 막은 곳(이름 → 이유). 시험과 로그가 본다.
        self.blocked: List[str] = []
        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: Set["asyncio.Task[None]"] = set()

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, limit=HEAD_LIMIT * 2)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    @property
    def exhausted(self) -> bool:
        return self.received >= self.budget

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._handle(reader, writer)
        finally:
            if task is not None:
                self._tasks.discard(task)
            writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self.idle)
        except asyncio.LimitOverrunError:
            await _answer(writer, 431, "요청 머리가 너무 길다")
            return
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            return  # 크롬이 요청 없이 닫았다 — 보낼 것도 남길 것도 없다
        if len(head) > HEAD_LIMIT:
            await _answer(writer, 431, "요청 머리가 너무 길다")
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            await _answer(writer, 400, "요청 줄을 못 읽었다")
            return
        method, target, version = parts
        if self.exhausted:
            logger.warning("브라우저가 받는 양 상한(%d바이트)에 닿아 막음: %s", self.budget, target)
            await _answer(writer, 503, "받는 양 상한에 닿았다")
            return
        if method.upper() == "CONNECT":
            where = _authority(target)
            if where is None:
                await _answer(writer, 400, "CONNECT 대상을 못 읽었다")
                return
            host, port = where
            upstream = await self._reach(writer, host, port)
            if upstream is None:
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._relay(reader, writer, upstream)
            return
        url = urlsplit(target)
        if url.scheme != "http" or not url.hostname:
            # https 는 CONNECT 로 온다. 그 밖의 것(절대 주소가 아닌 것, ftp 등)은 받지 않는다.
            await _answer(writer, 400, "http 절대 주소만 받는다")
            return
        try:
            port = url.port or 80
        except ValueError:
            await _answer(writer, 400, "포트를 못 읽었다")
            return
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        kept = [line for line in lines[1:] if line and _name(line) not in _HOP]
        length = _length(kept)
        if length is None or length > BODY_LIMIT:
            # 길이를 모르는 본문(chunked)이나 큰 본문은 넘기지 않는다 — 어디까지가 이 요청인지
            # 알아야 그 뒤(같은 연결의 다음 요청)를 검사 없이 흘리지 않는다. 붙기 전에 거절한다.
            await _answer(writer, 501, "길이를 모르거나 큰 본문은 넘기지 않는다")
            return
        try:
            body = (
                await asyncio.wait_for(reader.readexactly(length), timeout=self.idle)
                if length
                else b""
            )
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            return  # 크롬이 본문을 다 보내지 않고 끊었다
        upstream = await self._reach(writer, url.hostname, port)
        if upstream is None:
            return
        request = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""])
        try:
            # 이 요청 하나(머리 + 본문 길이만큼)만 넘긴다. 같은 연결에 이어 온 것은 읽지도 않는다.
            upstream[1].write(request.encode("latin-1") + body)
            await upstream[1].drain()
            await self._respond(upstream[0], writer)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
            return  # 어느 쪽이 끊었다 — 크롬은 그 요청을 실패로 본다
        finally:
            upstream[1].close()

    async def _respond(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        """사이트의 답을 크롬에 넘긴다. 머리에 `Connection: close` — 크롬이 이 연결로 다음 요청(다른
        사이트일 수도 있다)을 보내지 않게. 평문 HTTP 는 연결 하나에 요청 하나다(JAR-036 리뷰)."""
        try:
            head = await asyncio.wait_for(source.readuntil(b"\r\n\r\n"), timeout=self.idle)
        except asyncio.LimitOverrunError:
            await _answer(sink, 502, "사이트의 답 머리가 너무 길다")
            return
        self.received += len(head)
        lines = head.decode("latin-1").split("\r\n")
        kept = [line for line in lines[1:] if line and _name(line) not in _HOP]
        sink.write("\r\n".join([lines[0], *kept, "Connection: close", "", ""]).encode("latin-1"))
        await self._pipe(source, sink, counted=True)

    async def _reach(self, writer: asyncio.StreamWriter, host: str, port: int) -> Optional[Streams]:
        """이름을 한 번 풀어 검사하고, 검사한 주소로 붙는다. 못 붙으면 크롬에 답하고 None."""
        if port not in self.ports:
            self.blocked.append(host[:80])
            logger.warning("브라우저가 열지 않는 포트로 가려 했다 — 막음: %.80s:%d", host, port)
            await _answer(writer, 403, "80·443 포트만 연다")
            return None
        try:
            addresses = await checked(host, port, self.lookup)
        except Blocked as error:
            self.blocked.append(host[:80])
            logger.warning(
                "브라우저가 갈 수 없는 곳으로 가려 했다 — 막음: %.80s:%d (%s)", host, port, error
            )
            await _answer(writer, 403, "내부 주소는 열지 않는다")
            return None
        last: Optional[BaseException] = None
        for address in addresses:
            try:
                return await asyncio.wait_for(
                    self.opener(address.split("%", 1)[0], port), timeout=CONNECT_TIMEOUT
                )
            except (OSError, asyncio.TimeoutError) as error:
                last = error
        logger.info("브라우저 연결 실패: %s:%d (%s)", host, port, last)
        await _answer(writer, 502, "연결하지 못했다")
        return None

    async def _relay(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, upstream: Streams
    ) -> None:
        up_reader, up_writer = upstream
        try:
            await asyncio.gather(
                self._pipe(reader, up_writer, counted=False),
                self._pipe(up_reader, writer, counted=True),
            )
        finally:
            up_writer.close()

    async def _pipe(
        self, source: asyncio.StreamReader, sink: asyncio.StreamWriter, counted: bool
    ) -> None:
        """한 방향으로 흘린다. 어느 쪽이든 끊기거나 조용하면 끝 — 반대편도 닫아 함께 끝나게."""
        try:
            while True:
                chunk = await asyncio.wait_for(source.read(65_536), timeout=self.idle)
                if not chunk:
                    return
                if counted:
                    self.received += len(chunk)
                    if self.received > self.budget:
                        logger.warning("브라우저가 받는 양 상한(%d바이트)을 넘어 끊음", self.budget)
                        return
                sink.write(chunk)
                await sink.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            # 끊김·조용함은 연결의 끝이다(실패가 아니다). 크롬은 그 요청을 실패로 본다.
            return
        finally:
            sink.close()


def _authority(target: str) -> Optional[Tuple[str, int]]:
    """CONNECT 대상 'host:port' 또는 '[v6]:port'."""
    if target.startswith("["):
        end = target.find("]")
        if end < 0 or not target[end + 1 :].startswith(":"):
            return None
        host, port_text = target[1:end], target[end + 2 :]
    else:
        host, _, port_text = target.rpartition(":")
    if not host or not port_text.isdigit() or not 0 < int(port_text) < 65_536:
        return None
    return host, int(port_text)


def _name(line: str) -> str:
    return line.split(":", 1)[0].strip().lower()


def _length(headers: List[str]) -> Optional[int]:
    """본문 길이. 없으면 0, 모르거나(chunked·여러 값·숫자 아님) 이상하면 None."""
    if any(_name(line) == "transfer-encoding" for line in headers):
        return None
    values = [line.split(":", 1)[1].strip() for line in headers if _name(line) == "content-length"]
    if not values:
        return 0
    if len(set(values)) != 1 or not values[0].isdigit():
        return None
    return int(values[0])


async def _answer(writer: asyncio.StreamWriter, status: int, why: str) -> None:
    body = why.encode("utf-8")
    reason = {
        400: "Bad Request",
        403: "Forbidden",
        431: "Request Header Fields Too Large",
        501: "Not Implemented",
        503: "Service Unavailable",
    }.get(status, "Bad Gateway")
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("latin-1")
        + body
    )
    try:
        await writer.drain()
    except ConnectionError:
        return  # 크롬이 먼저 닫았다
