"""웹 — 자비스가 스스로 찾아보고 읽는 손.

검색은 데스크탑에 떠 있는 SearXNG 에 묻는다(2026-11-05 사용자 결정 — 무료, 계정 없음). 요청마다
엔진을
고른다: 구글(요약에 일정 같은 숫자가 잘 실린다)과 네이버(한국어 공식 공지·블로그).
덕덕고·스타트페이지는
캡차, Brave 는 요청 한도에 막혀 있었다(2026-11-05 실측).

**검색어는 밖으로 나간다**(SearXNG 를 거쳐 구글·네이버로) — 사용자가 원한 것이다. 그래서 나가는
검색어와
읽는 주소는 전부 로그에 남긴다.

페이지 읽기는 두 겹으로 막는다.
  - 내부 주소(사설·루프백·Tailscale 대역)는 읽지 않는다. 자비스는 vLLM·SearXNG 와 같은 기계에
  있어서,
    웹 글에 든 지시가 모델을 시켜 내부 주소를 읽게 하면 그 내용이 말로 새어 나간다. 리다이렉트도
    한 번씩 본다.
  - 이번 검색 결과에 나온 주소만 읽는다(`tools.py`). 모델의 맥락에는 사용자의 기록이 있다 —
    웹 글이 "이 주소를 읽어라(?q=기록)"라고 시켜도 기록이 밖으로 나가지 않게.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Awaitable, Callable, List, Optional, Protocol, Sequence, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

# 구글은 요약에 숫자(일정·가격)가 잘 실리고, 네이버는 한국어 공식 공지와 블로그를 앞에 둔다.
ENGINES = "google cse,naver"
# 사이트들이 이름 없는 클라이언트를 막는 일이 잦다. 어디서 왔는지는 밝힌다.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) jarvis-reader/1.0"
# 받는 양의 상한. 본문은 이보다 훨씬 짧게 자르지만, 끝없이 내려받지 않게.
MAX_BYTES = 2_000_000
# Tailscale 은 CGNAT 대역(100.64.0.0/10)을 쓴다 — is_private 가 이 대역을 안 잡는다.
TAILNET = ipaddress.ip_network("100.64.0.0/10")


@dataclass(frozen=True)
class Found:
    """검색 결과 하나."""

    title: str
    url: str
    snippet: str
    engine: str


@dataclass(frozen=True)
class Page:
    """읽은 페이지. `text` 는 본문만 추린 글(잘렸으면 `cut`)."""

    url: str
    title: str
    text: str
    cut: bool


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
    """주소 하나를 받아 본문을 글로. 내부망은 안 읽고, 리다이렉트도 한 번씩 확인한다."""

    client: httpx.AsyncClient
    max_chars: int = 4000
    max_redirects: int = 5
    resolve: Resolve = resolve

    async def read(self, url: str) -> Page:
        target = mobile(url)
        logger.info("페이지 읽기(밖으로 나감): %s", target)
        for _ in range(self.max_redirects + 1):
            await ensure_public(target, self.resolve)
            response = await self.client.get(
                target, headers={"User-Agent": USER_AGENT}, follow_redirects=False
            )
            if response.is_redirect and response.headers.get("location"):
                target = urljoin(target, response.headers["location"])
                continue
            response.raise_for_status()
            kind = response.headers.get("content-type", "")
            if "html" not in kind and "text/plain" not in kind:
                raise ValueError(f"글이 아니다({kind or '종류 모름'}): {target}")
            raw = response.content[:MAX_BYTES]
            html = decode(raw, response.headers.get("content-type", ""))
            title, text = extract(html) if "html" in kind else ("", _squash_lines(html))
            cut = len(text) > self.max_chars
            return Page(url=target, title=title, text=text[: self.max_chars], cut=cut)
        raise ValueError(f"리다이렉트가 {self.max_redirects}번을 넘었다: {url}")


def mobile(url: str) -> str:
    """네이버 블로그는 본문이 iframe 안에 있다 — 모바일 주소는 본문을 그대로 준다."""
    parts = urlsplit(url)
    if parts.hostname in ("blog.naver.com", "www.blog.naver.com"):
        return url.replace(f"//{parts.netloc}", "//m.blog.naver.com", 1)
    return url


async def ensure_public(url: str, lookup: Resolve = resolve) -> None:
    """http(s) 이고, 이름이 가리키는 모든 주소가 공개망이어야 한다. 아니면 `Blocked`."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Blocked(f"http(s) 주소가 아니다: {url}")
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = await lookup(host, port)
    except OSError as error:
        raise Blocked(f"이름을 못 찾았다: {host}") from error
    if not addresses:
        raise Blocked(f"이름을 못 찾았다: {host}")
    for raw in addresses:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
        if not _public(address):
            raise Blocked(f"내부 주소는 읽지 않는다: {host} → {address}")


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
_SKIP = {
    "script", "style", "noscript", "svg", "head", "header", "footer", "nav", "aside",
    "form", "iframe", "button", "select", "template",
}  # fmt: skip
_BLOCK = {
    "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table", "section", "article",
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

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag in _VOID:
            if tag == "br":
                self.parts.append(("\n", False, self.main > 0))
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
        elif tag in _BLOCK:
            self.parts.append(("\n", False, self.main > 0))

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
            elif open_tag in _BLOCK:
                self.parts.append(("\n", False, self.main > 0))
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
            line = _squash("".join(text))
            if line:
                lines.append((line, linked >= 0.8 * len(line)))
            text, linked = [], 0
            continue
        text.append(data)
        if in_link:
            linked += len(_squash(data))
    line = _squash("".join(text))
    if line:
        lines.append((line, linked >= 0.8 * len(line)))
    return lines


def _drop_menus(lines: Sequence[Tuple[str, bool]], short: int = 20, run: int = 3) -> List[str]:
    """메뉴 같은 줄(짧거나 거의 다 링크)이 `run` 개 넘게 이어진 덩어리를 뺀다. 본문 사이의 짧은
    줄(소제목)이나 링크 한두 줄은 남긴다."""
    kept: List[str] = []
    streak: List[str] = []
    for line, linked in lines:
        if linked or len(line) < short:
            streak.append(line)
            continue
        if len(streak) <= run:
            kept.extend(streak)
        streak = []
        kept.append(line)
    if len(streak) <= run:
        kept.extend(streak)
    return kept


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _squash_lines(text: str) -> str:
    return "\n".join(line for line in (_squash(x) for x in text.split("\n")) if line)
