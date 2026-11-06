"""웹 — 자비스가 스스로 찾아보고 읽는 손.

검색은 데스크탑에 떠 있는 SearXNG 에 묻는다(2026-11-05 사용자 결정 — 무료, 계정 없음). 요청마다
엔진을 고른다: 구글(요약에 일정 같은 숫자가 잘 실린다)과 네이버(한국어 공식 공지·블로그).
덕덕고·스타트페이지는 캡차, Brave 는 요청 한도에 막혀 있었다(2026-11-05 실측).

**검색어는 밖으로 나간다**(SearXNG 를 거쳐 구글·네이버로) — 사용자가 원한 것이다. 그래서 나가는
검색어와 읽는 주소는 전부 로그에 남긴다.

페이지 읽기는 두 겹으로 막는다.
  - 내부 주소(사설·루프백·Tailscale 대역)는 읽지 않는다. 자비스는 vLLM·SearXNG 와 같은 기계에
    있어서, 웹 글에 든 지시가 모델을 시켜 내부 주소를 읽게 하면 그 내용이 말로 새어 나간다.
    리다이렉트도 한 번씩 보고, 실제로 막는 건 접속하는 층이다(`PinnedBackend`).
  - 이번 검색 결과에 나온 주소와, 읽은 페이지에 있던 같은 사이트의 첨부 주소만 읽는다 — 모델이
    쓴 글자가 아니라 받은 그대로의 주소로(`tools.py`). 모델의 맥락에는 사용자의 기록이 있다 —
    웹 글이 "이 주소를 읽어라(?q=기록)"라고 시켜도 기록이 밖으로 나가지 않게.

받은 파일(PDF·한글·오피스)은 별도 프로세스에서 글만 뽑는다(`files.py`).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit

import httpcore
import httpx

from src.brain import files

logger = logging.getLogger(__name__)

# 구글은 요약에 숫자(일정·가격)가 잘 실리고, 네이버는 한국어 공식 공지와 블로그를 앞에 둔다.
ENGINES = "google cse,naver"
# 사이트들이 이름 없는 클라이언트를 막는 일이 잦다. 어디서 왔는지는 밝힌다.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) jarvis-reader/1.0"
# 받는 양의 상한. 본문은 이보다 훨씬 짧게 자르지만, 끝없이 내려받지 않게.
MAX_BYTES = 2_000_000
# 파일(PDF·한글·오피스)은 더 크다 — 공문 PDF 가 수 MB 다.
MAX_FILE_BYTES = 15_000_000
# 받기 전에 거절하는 종류. 글을 뽑을 수 없다.
_NEVER = ("image/", "video/", "audio/", "font/")
# Tailscale 은 CGNAT 대역(100.64.0.0/10)을 쓴다 — is_private 가 이 대역을 안 잡는다.
TAILNET = ipaddress.ip_network("100.64.0.0/10")
# 이 집의 공인 주소를 알려 주는 곳(https, 글 한 줄). 앞에서부터 묻는다.
ECHOES = ("https://api.ipify.org", "https://ipv4.icanhazip.com", "https://checkip.amazonaws.com")


class OwnAddress:
    """이 집(서버가 붙은 공유기)의 공인 주소. 그 주소로 가면 공유기가 안으로 되돌린다(NAT 헤어핀).

    사설 대역만 막아서는 못 막는다 — 공인 주소로 돌아 들어온 연결이 공유기 관리 화면과 접속 기기
    목록을 로그인 없이 열었다(JAR-036 리뷰 재현: 58.x:8080 → 공유기 로그인·접속 기기 목록 200). 알려
    주는 곳에 물어 `fresh` 초 동안 쓰고, 다시 못 물으면 마지막으로 안 것을 `keep` 초까지 쓴다. 한
    번도 모르면 None — 그땐 아무 데도 붙지 않는다(`checked`). 묻는 곳은 이 집의 공인 주소만
    안다(어느 사이트든 이미 보는 값이다).
    """

    def __init__(self, fresh: float = 1800.0, keep: float = 6 * 3600.0) -> None:
        self.fresh = fresh
        self.keep = keep
        self.address: Optional[str] = None
        self.learned = 0.0
        # 시험이 끼우는 값(네트워크를 안 탄다).
        self.fixed: Optional[str] = None
        self._lock: Optional[asyncio.Lock] = None

    async def get(self) -> Optional[str]:
        if self.fixed is not None:
            return self.fixed
        if self.address and time.monotonic() - self.learned < self.fresh:
            return self.address
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time.monotonic()
            if self.address and now - self.learned < self.fresh:
                return self.address
            found = await self._ask()
            if found:
                if found != self.address:
                    logger.info("이 집의 공인 주소: %s (그리로는 붙지 않는다)", found)
                self.address, self.learned = found, now
                return found
            if self.address and now - self.learned < self.keep:
                logger.warning(
                    "공인 주소를 다시 못 물었다 — 마지막으로 안 %s 를 쓴다", self.address
                )
                return self.address
            logger.error(
                "이 집의 공인 주소를 몰라 밖으로 붙지 않는다(공유기로 돌아 들어가는 길을 못 막는다)"
            )
            return None

    async def _ask(self) -> Optional[str]:
        async with httpx.AsyncClient(timeout=5.0, headers={"User-Agent": USER_AGENT}) as client:
            for url in ECHOES:
                try:
                    answer = (await client.get(url)).text.strip()
                    address = ipaddress.ip_address(answer)
                except (httpx.HTTPError, ValueError) as error:
                    logger.warning("공인 주소를 못 물었다: %s (%s)", url, type(error).__name__)
                    continue
                if isinstance(address, ipaddress.IPv4Address) and _public(address):
                    return str(address)
                logger.warning("공인 주소 답이 이상하다: %s → %.40s", url, answer)
        return None


OWN = OwnAddress()


@dataclass(frozen=True)
class Found:
    """검색 결과 하나."""

    title: str
    url: str
    snippet: str
    engine: str


@dataclass(frozen=True)
class Link:
    """페이지 안의 문서 링크(첨부). 읽은 페이지에 그대로 있던 주소라 다음 차례에 읽을 수 있다."""

    url: str
    name: str


@dataclass(frozen=True)
class Page:
    """읽은 페이지(또는 파일).

    `text` 는 본문만 추린 글(잘렸으면 `cut`), `links` 는 첨부·문서 링크.
    """

    url: str
    title: str
    text: str
    cut: bool
    links: Tuple[Link, ...] = ()
    kind: str = "html"


class Search(Protocol):
    async def search(self, query: str) -> List[Found]: ...


class Reader(Protocol):
    async def read(self, url: str) -> Page: ...


# 이름 → 주소들. 시험에서 바꿔 끼운다(실제 DNS 를 안 타게).
Resolve = Callable[[str, int], Awaitable[List[str]]]


async def resolve(host: str, port: int) -> List[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port)
    return [str(info[4][0]) for info in infos]


class Blocked(Exception):
    """읽으면 안 되는 주소(내부망, http 아닌 것)."""


@dataclass
class SearxSearch:
    """SearXNG 의 JSON 응답. 결과는 주소로 겹침을 걷어 위에서부터 `limit` 개."""

    base_url: str
    client: httpx.AsyncClient
    limit: int = 6
    engines: str = ENGINES
    language: str = "ko"

    async def search(self, query: str) -> List[Found]:
        logger.info("웹 검색(밖으로 나감): %s", query)
        response = await self.client.get(
            f"{self.base_url.rstrip('/')}/search",
            params={
                "q": query,
                "format": "json",
                "language": self.language,
                "engines": self.engines,
            },
        )
        response.raise_for_status()
        data = response.json()
        dead = data.get("unresponsive_engines") or []
        if dead:
            # 엔진 하나가 막혀도 나머지로 답할 수 있다. 다만 막힌 줄 모르고 결과가 줄면 품질이 왜
            # 떨어졌는지 모른다.
            logger.warning("검색 엔진 응답 없음: %s", dead)
        found: List[Found] = []
        seen = set()
        for item in data.get("results") or []:
            url = str(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            engines = item.get("engines") or [item.get("engine") or ""]
            found.append(
                Found(
                    title=_squash(str(item.get("title") or "")),
                    url=url,
                    snippet=_squash(str(item.get("content") or "")),
                    engine=str(engines[0] if engines else ""),
                )
            )
            if len(found) >= self.limit:
                break
        logger.info("검색 결과 %d개: %s", len(found), query)
        return found


@dataclass
class PageReader:
    """주소 하나를 받아 본문을 글로. 내부망은 안 읽고, 리다이렉트도 한 번씩 확인한다.

    `client` 는 `reader_client()` 로 만든다 — 접속하는 순간 검사한 주소로만 붙는다(아래).
    """

    client: httpx.AsyncClient
    max_chars: int = 4000
    max_redirects: int = 5
    resolve: Resolve = resolve
    # 받는 양과 시간의 상한. 다 받고 나서 자르면 63MB 를 끝까지 받았고, 1초에 1바이트씩 흘리는
    # 서버는 읽기 한도(한 번 읽기 기준)에 안 걸려 끝없이 읽었다(JAR-031 리뷰 재현).
    max_bytes: int = MAX_BYTES
    file_bytes: int = MAX_FILE_BYTES
    # 파일은 받고 나서 글을 뽑는 데도 시간이 든다(별도 프로세스에서, `parse_seconds` 안에) — 넉넉히.
    deadline: float = 40.0
    parse_seconds: float = 25.0

    async def read(self, url: str) -> Page:
        return await asyncio.wait_for(self._read(url), timeout=self.deadline)

    async def _read(self, url: str) -> Page:
        target = mobile(url)
        logger.info("페이지 읽기(밖으로 나감): %s", target)
        for _ in range(self.max_redirects + 1):
            # 먼저 한 번 본다(http 인지, 이름이 공개망인지) — 실제로 막는 건 접속하는 층이다.
            await ensure_public(target, self.resolve)
            async with self.client.stream(
                "GET", target, headers={"User-Agent": USER_AGENT}, follow_redirects=False
            ) as response:
                if response.is_redirect and response.headers.get("location"):
                    target = urljoin(target, response.headers["location"])
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                # 받기 전에 본다 — 글을 뽑을 수 없는 종류는 내려받지 않는다.
                if content_type.lower().startswith(_NEVER):
                    raise ValueError(f"글이 아니다({content_type}): {target}")
                name = files.name_of(target, response.headers.get("content-disposition", ""))
                lowered = content_type.lower()
                # 글(HTML·text·json·csv)은 이 프로세스에서 정리하니 작은 상한으로. json 을 파일
                # 상한(15MB)으로 받았더니 루프가 1.75초 멈췄다(3차 리뷰).
                textual = "html" in lowered or lowered.startswith("text/")
                textual = textual or "json" in lowered or "csv" in lowered
                limit = self.max_bytes if textual else self.file_bytes
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) >= limit:
                        break
            data = bytes(raw[:limit])
            kind = files.kind_of(data[:8], content_type, name)
            if kind in (files.HTML, files.TEXT):
                data = data[: self.max_bytes]  # 이름으로만 글인 것(octet-stream 의 .csv)도
            if kind == files.HTML:
                html = decode(data, content_type)
                title, text = extract(html)
                links = links_of(html, target)
            elif kind == files.TEXT:
                title, text, links = name, _squash_lines(decode(data, content_type)), []
            elif kind:
                # 파일 분석은 별도 프로세스에서 — 메모리·CPU 상한, 시간을 넘기면 끝낸다
                # (files 머리말).
                text = await files.extract(data, kind, timeout=self.parse_seconds)
                title, links = name, []
                logger.info("파일 읽음(%s, %d바이트): %s", kind, len(data), name)
            else:
                raise ValueError(f"읽을 수 없는 종류다({content_type or '모름'}): {target}")
            cut = len(text) > self.max_chars
            return Page(
                url=target,
                title=title,
                text=text[: self.max_chars],
                cut=cut,
                links=tuple(links),
                kind=kind,
            )
        raise ValueError(f"리다이렉트가 {self.max_redirects}번을 넘었다: {url}")


def reader_client(lookup: Resolve = resolve) -> httpx.AsyncClient:
    """페이지 읽기용 클라이언트. 접속하는 순간에만 이름을 풀어 검사하고 그 주소로 붙는다."""
    return httpx.AsyncClient(
        transport=PinnedTransport(lookup), timeout=httpx.Timeout(20.0, connect=5.0)
    )


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """TCP 를 여는 순간 이름을 한 번 풀어 검사하고, 검사한 주소로 붙는다.

    검사와 접속에서 이름을 따로 풀면 그 사이 내부 주소로 바뀔 수 있다(DNS 리바인딩 — JAR-031
    리뷰). 이 서버에는 clickhouse·redis·postgres·langfuse 같은 로컬 서비스가 떠 있다. 처음엔 URL 의
    호스트를 IP 로 바꿔 고정했더니 위층(쿠키·연결 풀·Host)까지 IP 를 보게 되어 도메인 쿠키가
    버려지고
    (브런치 리다이렉트가 끝없이), 같은 IP 의 다른 사이트가 남의 TLS 연결로 나갔다(421, 재리뷰 재현).
    그래서 고정은 이 층에서만 한다 — URL·Host·SNI·쿠키·연결 풀은 이름 그대로다.
    """

    def __init__(
        self, lookup: Resolve = resolve, inner: Optional[httpcore.AsyncNetworkBackend] = None
    ) -> None:
        self.lookup = lookup
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Optional[Iterable[Any]] = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await checked(host, port, self.lookup)
        return await self.inner.connect_tcp(
            addresses[0].split("%", 1)[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: Optional[float] = None,
        socket_options: Optional[Iterable[Any]] = None,
    ) -> httpcore.AsyncNetworkStream:
        raise Blocked("유닉스 소켓은 읽지 않는다")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class PinnedTransport(httpx.AsyncBaseTransport):
    """`PinnedBackend` 로 접속하는 전송. httpx 의 기본 전송과 같은 일을 공개 API 로만 한다."""

    def __init__(
        self, lookup: Resolve = resolve, inner: Optional[httpcore.AsyncNetworkBackend] = None
    ) -> None:
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            network_backend=PinnedBackend(lookup, inner),
            keepalive_expiry=5.0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,  # type: ignore[arg-type]
            extensions=request.extensions,
        )
        response = await self.pool.handle_async_request(core)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_Body(response.stream),  # type: ignore[arg-type]
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.pool.aclose()


class _Body(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self._stream:
            yield part

    async def aclose(self) -> None:
        if hasattr(self._stream, "aclose"):
            await self._stream.aclose()


def mobile(url: str) -> str:
    """네이버 블로그는 본문이 iframe 안에 있다 — 모바일 주소는 본문을 그대로 준다."""
    parts = urlsplit(url)
    if parts.hostname in ("blog.naver.com", "www.blog.naver.com"):
        return url.replace(f"//{parts.netloc}", "//m.blog.naver.com", 1)
    return url


async def ensure_public(url: str, lookup: Resolve = resolve) -> List[str]:
    """http(s) 이고, 이름이 가리키는 모든 주소가 공개망이어야 한다. 아니면 `Blocked`."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Blocked(f"http(s) 주소가 아니다: {url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return await checked(parts.hostname, port, lookup)


async def checked(host: str, port: int, lookup: Resolve = resolve) -> List[str]:
    """이름이 가리키는 주소들. 하나라도 공개망이 아니거나 이 집의 공인 주소면 `Blocked`."""
    try:
        addresses = await lookup(host, port)
    except (OSError, UnicodeError, ValueError) as error:
        # 64자 넘는 이름 조각 같은 것은 OSError 가 아니라 UnicodeError 로 온다 — 놓치면 답 없이
        # 끊기고 "Task exception was never retrieved" 가 남았다(JAR-036 리뷰).
        raise Blocked(f"이름을 못 찾았다: {host[:80]}") from error
    if not addresses:
        raise Blocked(f"이름을 못 찾았다: {host[:80]}")
    own = await OWN.get()
    if own is None:
        raise Blocked("이 집의 공인 주소를 몰라 붙지 않는다(공유기로 돌아 들어가는 길을 못 막는다)")
    mine = ipaddress.ip_address(own)
    for raw in addresses:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
        if not _public(address):
            raise Blocked(f"내부 주소는 읽지 않는다: {host[:80]} → {address}")
        if _v4(address) == mine:
            raise Blocked(f"이 집의 공인 주소는 읽지 않는다(공유기로 돌아 들어간다): {host[:80]}")
    return addresses


def _v4(address: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> Any:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _public(address: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_private or address.is_loopback or address.is_link_local:
        return False
    if address.is_reserved or address.is_multicast or address.is_unspecified:
        return False
    return not (isinstance(address, ipaddress.IPv4Address) and address in TAILNET)


_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", re.IGNORECASE)


def decode(raw: bytes, content_type: str) -> str:
    """헤더 → 문서 첫머리의 meta → utf-8 순으로 글자셋을 정한다.

    한국 사이트에 아직 euc-kr 이 있다.
    """
    charset = ""
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    else:
        meta = _CHARSET.search(raw[:4096])
        if meta:
            charset = meta.group(1).decode("ascii", "replace")
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# 본문이 아닌 곳. 메뉴·광고·스크립트가 앞을 채우면 잘린 본문에 정작 내용이 안 남는다.
# form 은 버리지 않는다 — 한국 공공 사이트는 페이지 전체를 form 으로 감싸는 일이 흔하고(큐넷 연간
# 시험일정의 표가 form 안에 있었다 — 버렸더니 3천 자가 44자가 됐다, 2026-11-06 JAR-038 실측). form
# 안의 입력칸은 글이 없고, 고르기(select)·버튼은 따로 뺀다.
_SKIP = {
    "script", "style", "noscript", "svg", "head", "header", "footer", "nav", "aside",
    "iframe", "button", "select", "template",
}  # fmt: skip
# 표의 칸(td·th)은 줄을 나누지 않고 " | "로 잇는다 — 칸마다 줄을 나누면 일정표가 짧은 줄 더미가 되어
# 메뉴처럼 지워졌다(JAR-031 리뷰 재현: 일정 페이지 다섯 중 넷에서 날짜가 거의 다 빠졌다).
_CELL = {"td", "th"}
_BLOCK = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "dd", "dt",
}  # fmt: skip


# 본문을 감싸는 칸. 있으면 그 안만 읽는다 — 블로그는 본문 앞에 "관련 글" 목록이 길게 붙어,
# 앞에서부터
# 자르면 본문이 밀려났다(2026-11-05 티스토리 실측).
_MAIN_TAGS = {"article", "main"}
_MAIN_HINTS = ("se-main-container", "entry-content", "article-body", "post-content", "tt_article")
# 빈 요소. 여는 태그만 있고 닫는 태그가 안 와서 깊이를 세면 어긋난다.
_VOID = {"br", "img", "hr", "input", "meta", "link", "area", "base", "col", "source", "wbr"}


class _Text(HTMLParser):
    """글 조각을 (글, 링크 안인가, 본문 칸 안인가)로 모은다. 줄 나누기는 블록 태그에서."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.link = 0
        self.stack: List[Tuple[str, bool]] = []  # (태그, 본문 칸을 연 태그인가)
        self.main = 0
        self.parts: List[Tuple[str, bool, bool]] = []
        self.title: List[str] = []
        self.in_title = False
        # 표의 칸 안인가. 칸 안의 `<br>` 은 공백으로 — 한 행이 한 줄이어야 칸 순서가 맞는다. 칸
        # 안에서 줄을 나눴더니 큐넷 연간 시험일정의 행이 여러 줄로 쪼개져 발표일을 시험일로
        # 읽었다(2026-11-06 JAR-038 실측). 블록(p·div·tr)은 칸 안에서도 줄을 나눈다 — 페이지 전체가
        # 바깥 칸 안에 있는 표 레이아웃에서 메뉴·본문·안쪽 표가 한 줄로 뭉쳤다(JAR-036 리뷰 재현).
        self.cell = 0

    def _break(self) -> None:
        self.parts.append(("\n", False, self.main > 0))

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag in _VOID:
            if tag == "br":
                self.parts.append(("\n" if not self.cell else " ", False, self.main > 0))
            return
        names = " ".join(v or "" for k, v in attrs if k in ("class", "id"))
        opens = tag in _MAIN_TAGS or any(hint in names for hint in _MAIN_HINTS)
        self.stack.append((tag, opens))
        if opens:
            self.main += 1
        if tag in _SKIP:
            self.skip += 1
        elif tag == "a":
            self.link += 1
        elif tag in _CELL:
            self.cell += 1
            self.parts.append((" | ", False, self.main > 0))
        elif tag in _BLOCK:
            self._break()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in _VOID:
            return
        # 닫는 태그를 쌓인 것과 맞춘다. 안 닫힌 태그가 있으면 그것들까지 같이 닫는다.
        if not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            open_tag, opened = self.stack.pop()
            if opened:
                self.main -= 1
            if open_tag in _SKIP and self.skip:
                self.skip -= 1
            elif open_tag == "a" and self.link:
                self.link -= 1
            elif open_tag in _CELL and self.cell:
                self.cell -= 1
            elif open_tag in _BLOCK:
                self._break()
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
        elif not self.skip:
            self.parts.append((data, self.link > 0, self.main > 0))


def extract(html: str) -> Tuple[str, str]:
    """(제목, 본문). 본문 칸이 있으면 그 안만, 링크만으로 된 줄 덩어리(메뉴·관련 글)는 걷는다."""
    parser = _Text()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # 깨진 HTML 이 흔하다. 거기까지 읽은 것으로 간다 — 다만 알 수 있게 남긴다.
        logger.warning("HTML 을 끝까지 못 읽었다 — 읽은 데까지 쓴다", exc_info=True)
    title = _squash("".join(parser.title))
    main = _lines([p for p in parser.parts if p[2]])
    # 본문 칸이 짧으면(목록 페이지의 한 칸 등) 칸을 믿지 않고 전체를 본다.
    chosen = main if sum(len(t) for t, _ in main) >= 300 else _lines(parser.parts)
    return title, "\n".join(_drop_menus(chosen))


def _lines(parts: Sequence[Tuple[str, bool, bool]]) -> List[Tuple[str, bool]]:
    """조각을 줄로. 줄마다 (글, 거의 다 링크인가)."""
    lines: List[Tuple[str, bool]] = []
    text: List[str] = []
    linked = 0
    for data, in_link, _ in parts:
        if data == "\n":
            line = _squash("".join(text)).strip(" |")
            if line:
                lines.append((line, linked >= 0.8 * len(line)))
            text, linked = [], 0
            continue
        text.append(data)
        if in_link:
            linked += len(_squash(data))
    line = _squash("".join(text)).strip(" |")
    if line:
        lines.append((line, linked >= 0.8 * len(line)))
    return lines


def _drop_menus(lines: Sequence[Tuple[str, bool]], run: int = 3) -> List[str]:
    """거의 다 링크인 줄이 `run` 개 넘게 이어진 덩어리(메뉴, 관련 글 목록)만 뺀다.

    짧다는 것만으로는 빼지 않는다 — "1회 실기 접수: 3.24~3.27" 같은 일정 줄이 짧은 줄 더미로 몰려
    메뉴처럼 지워졌다(JAR-031 리뷰 재현).
    """
    kept: List[str] = []
    streak: List[str] = []
    for line, linked in lines:
        if linked:
            streak.append(line)
            continue
        if len(streak) <= run:
            kept.extend(streak)
        streak = []
        kept.append(line)
    if len(streak) <= run:
        kept.extend(streak)
    return kept


# 자바스크립트 버튼으로 된 첨부. 링크가 아니라서 따라갈 수 없다 — 사이트마다 실제 주소를 만든다.
# 큐넷: 공지의 일정이 본문이 아니라 첨부 PDF·HWP 에만 있었다(2026-11-06 실제 대화). 버튼은
# fileDownMan004('경로','이름','번호') 이고, 그 함수는 같은 페이지의 man004.do 를 GET 으로 부른다.
_QNET = re.compile(r"fileDownMan004\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)")
LINKS = 10
# 첨부 주소 길이 상한 — 풀어 쓴(%-디코딩한) 길이로 잰다. 긴 주소 하나로 본문 4천 자 상한을 우회해
# 문맥을 채울 수 있다(리뷰). 인코딩된 길이로 쟀더니 한글 1자가 9자라 큐넷 첨부 이름이 34자쯤만
# 돼도 조용히 빠졌다(재리뷰). 인코딩된 길이에도 따로 상한 — 풀어 쓴 길이만 보면 링크 열 개가
# 인코딩 4천 자씩, 첨부 목록 하나가 4만 자가 됐다(3차 리뷰). 큐넷 실측 가장 긴 것 441자.
LINK_LENGTH = 500
LINK_ENCODED = 1_000
_DOCUMENTS = (".pdf", ".hwp", ".hwpx", ".docx", ".xlsx", ".pptx", ".csv")


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: List[Tuple[str, str, str]] = []  # (href, onclick, 글)
        self.open: Optional[List[str]] = None
        self.text: List[str] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        values = dict(attrs)
        onclick = values.get("onclick") or ""
        if tag == "a" or onclick:
            self.open = [values.get("href") or "", onclick]
            self.text = []

    def handle_endtag(self, tag: str) -> None:
        if self.open is not None and tag in ("a", "button"):
            self.found.append((self.open[0], self.open[1], _squash("".join(self.text))))
            self.open = None

    def handle_data(self, data: str) -> None:
        if self.open is not None:
            self.text.append(data)


def links_of(html: str, base: str) -> List[Link]:
    """페이지 안의 문서 링크(첨부). 같은 사이트의 것만 — 파일 확장자가 붙은 링크, 링크 글이 파일
    이름인 것(확장자 없는 게시판 첨부), 아는 사이트의 첨부 버튼.

    남의 사이트 링크를 받으면 카페·블로그 글 하나가 공격자 주소 열 개를 허용 목록에 넣는다 —
    읽기마다 "어느 링크인가"만큼 값이 샌다(JAR-035 리뷰).
    """
    parser = _Anchors()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.warning("링크를 끝까지 못 읽었다 — 읽은 데까지 쓴다", exc_info=True)
    host = urlsplit(base).hostname
    out: List[Link] = []
    seen = set()
    too_long = 0
    query = dict(parse_qsl(urlsplit(base).query))
    for href, onclick, text in parser.found:
        url = ""
        name = text
        qnet = _QNET.search(onclick)
        if qnet:
            if not (query.get("ARTL_SEQ") and query.get("BOARD_ID")):
                continue
            path, name, seq = qnet.groups()
            url = urljoin(
                base,
                "man004.do?"
                + urlencode(
                    {
                        "id": "man00402s01",
                        "gSite": "Q",
                        "gId": "",
                        "fileCode": "R001",
                        "BOARD_ID": query["BOARD_ID"],
                        "ARTL_SEQ": query["ARTL_SEQ"],
                        "filePath": path,
                        "fileName": name,
                        "fileSeq": seq,
                        "href": "0",
                    }
                ),
            )
        elif href and not href.strip().lower().startswith(("javascript:", "#", "mailto:")):
            absolute = urljoin(base, href.strip())
            named = text.lower().endswith(_DOCUMENTS)
            if urlsplit(absolute).path.lower().endswith(_DOCUMENTS) or named:
                url = absolute
                name = text or files.name_of(absolute)
        parts = urlsplit(url)
        if not url or parts.scheme not in ("http", "https") or parts.hostname != host:
            continue
        if len(unquote(url)) > LINK_LENGTH or len(url) > LINK_ENCODED:
            too_long += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(Link(url=url, name=name[:80]))
        if len(out) >= LINKS:
            break
    if too_long:
        logger.info("첨부 링크 %d개는 주소가 %d자를 넘어 뺐다: %s", too_long, LINK_LENGTH, base)
    return out


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _squash_lines(text: str) -> str:
    return "\n".join(line for line in (_squash(x) for x in text.split("\n")) if line)
