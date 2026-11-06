"""브라우저가 나가는 문 — 공개 주소로만, 검사한 주소로만, 받는 양은 상한까지."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, List, Tuple

import pytest

from src.brain.egress import EgressProxy

Streams = Tuple[asyncio.StreamReader, asyncio.StreamWriter]


def _lookup(table: Dict[str, List[str]]) -> Callable[[str, int], Awaitable[List[str]]]:
    async def lookup(host: str, port: int) -> List[str]:
        return table[host]

    return lookup


async def _upstream(answer: bytes) -> Tuple[asyncio.AbstractServer, int, List[bytes]]:
    """받은 것을 적어 두고 정해 둔 답을 주는 서버(시험용 '밖')."""
    got: List[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(65_536)
        got.append(data)
        writer.write(answer)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], got


def _opener(port: int, dialed: List[Tuple[str, int]]) -> Callable[[str, int], Awaitable[Streams]]:
    """검사한 주소로 붙으려는 것을 적고, 로컬 시험 서버로 잇는다."""

    async def opener(host: str, wanted: int) -> Streams:
        dialed.append((host, wanted))
        return await asyncio.open_connection("127.0.0.1", port)

    return opener


async def _ask(proxy_url: str, raw: bytes) -> bytes:
    host, port = proxy_url.rsplit("//", 1)[1].split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65_536), timeout=5)
    writer.close()
    return data


PUBLIC = {"site.example": ["211.115.84.70"]}


@pytest.mark.parametrize(
    ("name", "address"),
    [
        ("localhost", "127.0.0.1"),
        ("router.example", "192.168.0.1"),
        ("mac.tailnet", "100.65.255.109"),
        ("v6.example", "::1"),
        ("mapped.example", "::ffff:100.98.90.38"),
    ],
)
def test_inside_addresses_are_refused_for_https_and_http(name: str, address: str) -> None:
    """자비스 :8100, Langfuse, 공유기, Tailscale 너머의 맥 — 브라우저는 열지 않는다."""

    async def run() -> Tuple[bytes, bytes, List[Tuple[str, int]], List[str]]:
        dialed: List[Tuple[str, int]] = []
        proxy = EgressProxy(lookup=_lookup({name: [address]}), opener=_opener(1, dialed))
        url = await proxy.start()
        try:
            tls = await _ask(url, f"CONNECT {name}:443 HTTP/1.1\r\nHost: {name}\r\n\r\n".encode())
            plain = await _ask(url, f"GET http://{name}:8100/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        finally:
            await proxy.close()
        return tls, plain, dialed, proxy.blocked

    tls, plain, dialed, blocked = asyncio.run(run())
    assert tls.startswith(b"HTTP/1.1 403") and plain.startswith(b"HTTP/1.1 403")
    assert dialed == [] and blocked == [name, name]


def test_a_literal_inside_address_is_refused_too() -> None:
    """이름 없이 주소를 바로 써도 같다(이름 풀기가 그 주소를 그대로 돌려준다)."""
    from src.brain.web import resolve

    async def run() -> bytes:
        proxy = EgressProxy(lookup=resolve, opener=_opener(1, []))
        url = await proxy.start()
        try:
            return await _ask(url, b"CONNECT [::1]:8100 HTTP/1.1\r\n\r\n")
        finally:
            await proxy.close()

    assert asyncio.run(run()).startswith(b"HTTP/1.1 403")


def test_a_tunnel_goes_to_the_checked_address_and_carries_bytes() -> None:
    """https 는 CONNECT 터널 — 문은 내용을 안 보고 양쪽으로 흘린다. 붙는 곳은 검사한 주소."""

    async def run() -> Tuple[bytes, List[Tuple[str, int]], List[bytes]]:
        server, port, got = await _upstream(b"server-hello")
        dialed: List[Tuple[str, int]] = []
        proxy = EgressProxy(lookup=_lookup(PUBLIC), opener=_opener(port, dialed))
        url = await proxy.start()
        try:
            host, proxy_port = url.rsplit("//", 1)[1].split(":")
            reader, writer = await asyncio.open_connection(host, int(proxy_port))
            writer.write(b"CONNECT site.example:443 HTTP/1.1\r\nHost: site.example:443\r\n\r\n")
            await writer.drain()
            established = await reader.readuntil(b"\r\n\r\n")
            writer.write(b"client-hello")
            await writer.drain()
            reply = await asyncio.wait_for(reader.read(100), timeout=5)
            writer.close()
        finally:
            await proxy.close()
            server.close()
        return established + reply, dialed, got

    answer, dialed, got = asyncio.run(run())
    assert answer.startswith(b"HTTP/1.1 200") and answer.endswith(b"server-hello")
    assert dialed == [("211.115.84.70", 443)] and got == [b"client-hello"]


def test_plain_http_is_rewritten_for_the_site_and_closed_after() -> None:
    """일반 HTTP 는 절대 주소로 온다 — 사이트에는 경로만, 프록시 헤더는 빼고, 한 요청 한 연결."""

    async def run() -> Tuple[bytes, List[bytes], List[Tuple[str, int]]]:
        server, port, got = await _upstream(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        dialed: List[Tuple[str, int]] = []
        proxy = EgressProxy(lookup=_lookup(PUBLIC), opener=_opener(port, dialed))
        url = await proxy.start()
        try:
            answer = await _ask(
                url,
                b"GET http://site.example/a?b=1 HTTP/1.1\r\nHost: site.example\r\n"
                b"Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\nAccept: */*\r\n\r\n",
            )
        finally:
            await proxy.close()
            server.close()
        return answer, got, dialed

    answer, got, dialed = asyncio.run(run())
    request = got[0].decode()
    assert answer.endswith(b"ok") and dialed == [("211.115.84.70", 80)]
    assert request.startswith("GET /a?b=1 HTTP/1.1\r\n") and "Host: site.example" in request
    assert "Proxy-Connection" not in request and "keep-alive" not in request
    assert "Connection: close" in request and "Accept: */*" in request


def test_a_name_that_turns_inward_after_the_check_is_not_followed() -> None:
    """DNS 리바인딩: 이름은 한 번만 풀고, 붙는 곳은 그때 검사한 주소다(다시 풀지 않는다)."""
    answers = iter([["211.115.84.70"], ["127.0.0.1"]])
    asked: List[str] = []

    async def lookup(host: str, port: int) -> List[str]:
        asked.append(host)
        return next(answers)

    async def run() -> List[Tuple[str, int]]:
        server, port, _ = await _upstream(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        dialed: List[Tuple[str, int]] = []
        proxy = EgressProxy(lookup=lookup, opener=_opener(port, dialed))
        url = await proxy.start()
        try:
            await _ask(url, b"GET http://site.example/ HTTP/1.1\r\nHost: site.example\r\n\r\n")
        finally:
            await proxy.close()
            server.close()
        return dialed

    assert asyncio.run(run()) == [("211.115.84.70", 80)] and asked == ["site.example"]


def test_what_comes_in_is_capped_per_session() -> None:
    """끝없이 흘리는 사이트로 메모리를 채우지 않게 — 상한을 넘으면 끊고, 그 뒤 요청은 거절한다."""

    async def run() -> Tuple[int, bytes, int]:
        big = b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 300_000
        server, port, _ = await _upstream(big)
        proxy = EgressProxy(lookup=_lookup(PUBLIC), opener=_opener(port, []), budget=100_000)
        url = await proxy.start()
        try:
            host, proxy_port = url.rsplit("//", 1)[1].split(":")
            reader, writer = await asyncio.open_connection(host, int(proxy_port))
            writer.write(b"GET http://site.example/ HTTP/1.1\r\nHost: site.example\r\n\r\n")
            await writer.drain()
            first = len(await asyncio.wait_for(reader.read(), timeout=5))
            writer.close()
            after = await _ask(url, b"GET http://site.example/ HTTP/1.1\r\nHost: s\r\n\r\n")
        finally:
            await proxy.close()
            server.close()
        return first, after, proxy.received

    first, after, received = asyncio.run(run())
    assert first <= 100_000 and received > 100_000
    assert after.startswith(b"HTTP/1.1 503")


@pytest.mark.parametrize(
    "raw",
    [
        b"GET /relative HTTP/1.1\r\nHost: site.example\r\n\r\n",
        b"GET ftp://site.example/x HTTP/1.1\r\n\r\n",
        b"CONNECT site.example HTTP/1.1\r\n\r\n",
        b"CONNECT site.example:99999 HTTP/1.1\r\n\r\n",
        b"NONSENSE\r\n\r\n",
    ],
)
def test_odd_requests_are_refused(raw: bytes) -> None:
    async def run() -> Tuple[bytes, List[Tuple[str, int]]]:
        dialed: List[Tuple[str, int]] = []
        proxy = EgressProxy(lookup=_lookup(PUBLIC), opener=_opener(1, dialed))
        url = await proxy.start()
        try:
            return await _ask(url, raw), dialed
        finally:
            await proxy.close()

    answer, dialed = asyncio.run(run())
    assert answer.startswith(b"HTTP/1.1 400") and dialed == []


def test_an_oversized_request_head_is_refused() -> None:
    async def run() -> bytes:
        proxy = EgressProxy(lookup=_lookup(PUBLIC), opener=_opener(1, []))
        url = await proxy.start()
        try:
            head = b"GET http://site.example/ HTTP/1.1\r\nX: " + b"a" * 20_000 + b"\r\n\r\n"
            return await _ask(url, head)
        finally:
            await proxy.close()

    assert asyncio.run(run()).startswith(b"HTTP/1.1 431")


def test_closing_ends_open_tunnels() -> None:
    """세션이 끝나면 열린 터널도 같이 닫힌다 — 남는 연결이 없게."""

    async def run() -> bytes:
        async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(30)

        server = await asyncio.start_server(hold, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        proxy = EgressProxy(lookup=_lookup(PUBLIC), opener=_opener(port, []))
        url = await proxy.start()
        host, proxy_port = url.rsplit("//", 1)[1].split(":")
        reader, writer = await asyncio.open_connection(host, int(proxy_port))
        writer.write(b"CONNECT site.example:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.wait_for(proxy.close(), timeout=5)
        rest = await asyncio.wait_for(reader.read(), timeout=5)
        server.close()
        return rest

    assert asyncio.run(run()) == b""
