"""웹 — 검색 결과 읽기, 내부망 막기, 본문 추리기."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List
from urllib.parse import parse_qsl, urlsplit

import httpcore
import httpx
import pytest

from src.brain.web import (
    Blocked,
    PageReader,
    PinnedTransport,
    SearxSearch,
    ensure_public,
    extract,
    mobile,
)


def _client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _public(*addresses: str):  # type: ignore[no-untyped-def]
    async def lookup(host: str, port: int) -> List[str]:
        return list(addresses)

    return lookup


def test_search_asks_both_engines_and_drops_repeats(caplog: pytest.LogCaptureFixture) -> None:
    """구글(요약에 숫자가 실린다)과 네이버(한국어 공지)를 같이.

    같은 주소는 한 번, 막힌 엔진은 남긴다.
    """
    caplog.set_level(logging.INFO, logger="src.brain.web")
    seen: Dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://a.kr/1",
                        "title": " 일정\n안내 ",
                        "content": "1회 1.12",
                        "engines": ["naver"],
                    },
                    {
                        "url": "https://a.kr/1",
                        "title": "같은 주소",
                        "content": "",
                        "engines": ["google cse"],
                    },
                    {
                        "url": "https://b.kr/2",
                        "title": "둘",
                        "content": "요약",
                        "engine": "google cse",
                    },
                    {"url": "https://c.kr/3", "title": "셋", "content": "", "engines": ["naver"]},
                ],
                "unresponsive_engines": [["brave", "too many requests"]],
            },
        )

    search = SearxSearch("http://searx:8888", _client(handler), limit=2)
    found = asyncio.run(search.search("정처기 실기 일정"))

    assert [f.url for f in found] == ["https://a.kr/1", "https://b.kr/2"]
    assert found[0].title == "일정 안내" and found[0].engine == "naver"
    assert seen["format"] == "json" and seen["engines"] == "google cse,naver"
    assert seen["q"] == "정처기 실기 일정"
    assert "검색 엔진 응답 없음" in caplog.text
    # 검색어는 밖으로 나간다 — 나간 것은 남긴다.
    assert "웹 검색(밖으로 나감): 정처기 실기 일정" in caplog.text


def test_a_broken_search_is_loud() -> None:
    search = SearxSearch("http://searx:8888", _client(lambda r: httpx.Response(502)))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(search.search("아무거나"))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.0.5",
        "100.98.90.38",
        "169.254.1.1",
        "::1",
        "::ffff:10.0.0.1",
        "::ffff:100.98.90.38",
    ],
)
def test_internal_addresses_are_never_read(address: str) -> None:
    """자비스는 vLLM·SearXNG 와 같은 기계다. 웹 글이 시킨 내부 주소의 내용이 말로 새면 안 된다.

    Tailscale 대역(100.64.0.0/10)은 is_private 가 안 잡는다 — 따로 막는다.
    """
    with pytest.raises(Blocked):
        asyncio.run(ensure_public("http://looks-public.example/x", _public(address)))


def test_public_addresses_pass_and_odd_schemes_do_not() -> None:
    asyncio.run(ensure_public("https://q-net.or.kr/a", _public("211.115.84.70")))
    with pytest.raises(Blocked):
        asyncio.run(ensure_public("file:///etc/passwd", _public("211.115.84.70")))
    with pytest.raises(Blocked):
        asyncio.run(ensure_public("https://nowhere.example/", _public()))


def test_a_redirect_into_the_inside_is_blocked() -> None:
    """공개 주소가 내부 주소로 돌려보내도 한 번씩 다시 본다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://inside.example/secret"})

    async def lookup(host: str, port: int) -> List[str]:
        return ["127.0.0.1"] if host == "inside.example" else ["211.115.84.70"]

    reader = PageReader(_client(handler), resolve=lookup)
    with pytest.raises(Blocked):
        asyncio.run(reader.read("https://outside.example/start"))


def test_only_text_pages_are_read() -> None:
    reader = PageReader(
        _client(
            lambda r: httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"%PDF"
            )
        ),
        resolve=_public("211.115.84.70"),
    )
    with pytest.raises(ValueError):
        asyncio.run(reader.read("https://a.kr/file.pdf"))


def test_old_korean_pages_are_decoded_and_cut() -> None:
    """한국 사이트에 아직 euc-kr 이 있다. 헤더에 글자셋이 없으면 문서의 meta 를 본다."""
    body = '<html><head><meta charset="euc-kr"><title>안내</title></head><body><p>' + "가" * 50
    raw = (body + "</p></body></html>").encode("euc-kr")
    reader = PageReader(
        _client(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=raw)),
        max_chars=10,
        resolve=_public("211.115.84.70"),
    )
    page = asyncio.run(reader.read("https://old.kr/"))
    assert page.title == "안내" and page.text == "가" * 10 and page.cut


def test_naver_blog_is_read_through_its_mobile_page() -> None:
    """데스크탑 블로그는 본문이 iframe 안이라 빈 틀만 온다."""
    assert mobile("https://blog.naver.com/abc/123") == "https://m.blog.naver.com/abc/123"
    assert mobile("https://m.blog.naver.com/abc/123") == "https://m.blog.naver.com/abc/123"
    assert mobile("https://www.q-net.or.kr/x") == "https://www.q-net.or.kr/x"


def test_the_article_is_kept_and_link_menus_are_dropped() -> None:
    """블로그는 본문 앞에 \"관련 글\" 목록이 길게 붙는다.

    앞에서부터 자르면 본문이 밀려났다(티스토리 실측).
    """
    related = "".join(
        f'<div><a href="/{i}">관련 글 {i}: 제목이 길어서 스무 자는 넘는 글</a></div>'
        for i in range(6)
    )
    html = (
        "<html><head><title>정처기 일정</title><script>var x=1</script></head><body>"
        "<nav><a>홈</a><a>메뉴</a></nav>"
        f"{related}"
        "<article><h2>필기</h2><p>" + "1회 필기 접수는 1월 12일부터 1월 15일까지다. " * 10 + "</p>"
        "<p>실기<br>3회 실기는 10월 24일부터다.</p></article>"
        "<footer>회사 소개</footer></body></html>"
    )
    title, text = extract(html)

    assert title == "정처기 일정"
    assert text.startswith("필기\n1회 필기 접수는")
    assert "관련 글" not in text and "메뉴" not in text and "회사 소개" not in text
    assert "var x" not in text
    assert "실기\n3회 실기는 10월 24일부터다." in text


def test_without_an_article_the_page_minus_menus_is_kept() -> None:
    html = (
        "<body><ul>" + "".join(f"<li><a>메뉴{i}</a></li>" for i in range(8)) + "</ul>"
        "<div><p>본문 문단은 이렇게 충분히 길게 쓰여 있다고 하자.</p><p>짧은 줄</p>"
        "<p>그다음 문단도 충분히 길게 쓰여 있다고 하자.</p></div></body>"
    )
    _, text = extract(html)
    assert "메뉴" not in text
    assert text.split("\n") == [
        "본문 문단은 이렇게 충분히 길게 쓰여 있다고 하자.",
        "짧은 줄",
        "그다음 문단도 충분히 길게 쓰여 있다고 하자.",
    ]


def test_schedule_tables_and_short_date_lines_survive() -> None:
    """칸마다 줄을 나누니 일정표가 짧은 줄 더미가 되어 메뉴처럼 지워졌다.

    실제 일정 페이지 다섯 중 넷에서 날짜가 거의 다 빠졌다(JAR-031 리뷰 재현). 표의 한 행은 한 줄로,
    짧아도 링크가 아니면 남긴다.
    """
    html = (
        "<body><ul>"
        + "".join(f'<li><a href="/{i}">관련 글 {i}</a></li>' for i in range(5))
        + "</ul>"
        "<div><p>1회 실기 접수: 3.24~3.27</p><p>2회 실기 접수: 6.23~6.26</p>"
        "<p>3회 실기 접수: 9.22~9.25</p><p>4회 없음</p><p>발표 12.18</p>"
        "<table><tr><th>회차</th><th>필기접수</th></tr><tr><td>1회</td><td>1.12~1.15</td></tr></table>"
        "</div></body>"
    )
    _, text = extract(html)
    assert "관련 글" not in text
    assert text.split("\n") == [
        "1회 실기 접수: 3.24~3.27",
        "2회 실기 접수: 6.23~6.26",
        "3회 실기 접수: 9.22~9.25",
        "4회 없음",
        "발표 12.18",
        "회차 | 필기접수",
        "1회 | 1.12~1.15",
    ]


def _streamed(chunks: List[bytes], seen: List[int], kind: str = "text/html", delay: float = 0.0):  # type: ignore[no-untyped-def]
    async def body():  # type: ignore[no-untyped-def]
        for i, chunk in enumerate(chunks):
            if delay:
                await asyncio.sleep(delay)
            seen.append(i)
            yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": kind}, content=body())

    return handler


def test_a_huge_page_is_not_downloaded_whole() -> None:
    """다 받고 나서 자르면 63MB 를 끝까지 받았다(리뷰 재현) — 상한에서 멈춘다."""
    seen: List[int] = []
    reader = PageReader(
        _client(_streamed([b"<p>" + b"a" * 1000 + b"</p>"] * 100, seen)),
        max_bytes=3000,
        resolve=_public("211.115.84.70"),
    )
    page = asyncio.run(reader.read("https://big.example/"))
    assert len(seen) <= 4 and page.text.startswith("a")


def test_a_body_without_text_is_refused_before_it_is_read() -> None:
    """그림·영상은 글을 뽑을 수 없다 — 받기 전에 거절한다(파일은 이제 읽는다)."""
    seen: List[int] = []
    reader = PageReader(
        _client(_streamed([b"\x89PNG"] * 50, seen, kind="image/png")),
        resolve=_public("211.115.84.70"),
    )
    with pytest.raises(ValueError):
        asyncio.run(reader.read("https://a.kr/photo.png"))
    assert seen == []


def test_a_drip_feeding_server_hits_the_deadline() -> None:
    """1초에 1바이트씩 흘리면 읽기 한도(한 번 읽기 기준)에 안 걸려 끝없이 읽었다(리뷰 재현)."""
    seen: List[int] = []
    reader = PageReader(
        _client(_streamed([b"a"] * 100, seen, delay=0.05)),
        deadline=0.2,
        resolve=_public("211.115.84.70"),
    )
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(reader.read("https://slow.example/"))
    assert len(seen) < 10


def test_the_reader_keeps_names_in_the_url() -> None:
    """URL·Host·쿠키는 이름 그대로다.

    URL 을 IP 로 바꿔 고정했더니 도메인 쿠키가 버려져 브런치 리다이렉트가 끝없이 돌았다(재리뷰
    재현). 데스크탑 블로그는 iframe 이라 모바일 주소로 읽는다.
    """
    asked: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>ok</p>")

    reader = PageReader(_client(handler), resolve=_public("211.115.84.70"))
    asyncio.run(reader.read("https://blog.naver.com/abc/123?x=1"))
    asyncio.run(reader.read("https://www.bücher.de/"))

    assert asked[0].url.host == "m.blog.naver.com" and asked[0].url.query == b"x=1"
    # 유니코드 이름은 punycode 로 — 이름을 헤더에 그대로 넣으면 보내기도 전에 터졌다(재리뷰).
    assert asked[1].headers["host"] == "www.xn--bcher-kva.de"


class _Recording(httpcore.AsyncMockBackend):
    """접속한 주소를 남기는 가짜 네트워크."""

    def __init__(self, buffer: List[bytes]) -> None:
        super().__init__(buffer)
        self.hosts: List[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # type: ignore[no-untyped-def]
        self.hosts.append(host)
        return await super().connect_tcp(host, port, timeout, local_address, socket_options)


OK_PAGE = [
    b"HTTP/1.1 200 OK\r\n",
    b"Content-Type: text/html\r\n",
    b"Content-Length: 9\r\n",
    b"\r\n",
    b"<p>ok</p>",
]


def test_connections_go_only_to_the_checked_address() -> None:
    """접속하는 순간 이름을 풀어 검사한 그 주소로 붙는다."""
    network = _Recording(OK_PAGE)
    client = httpx.AsyncClient(transport=PinnedTransport(_public("211.115.84.70"), network))
    page = asyncio.run(
        PageReader(client, resolve=_public("211.115.84.70")).read("http://outside.example/a")
    )
    assert page.text == "ok" and network.hosts == ["211.115.84.70"]


def test_a_name_that_turns_inward_after_the_check_is_still_blocked() -> None:
    """DNS 리바인딩: 먼저 볼 땐 공개망, 접속할 땐 127.0.0.1 — 접속하는 층이 막는다(재리뷰)."""
    answers = [["211.115.84.70"], ["127.0.0.1"]]

    async def flip(host: str, port: int) -> List[str]:
        return answers.pop(0) if len(answers) > 1 else answers[0]

    network = _Recording(OK_PAGE)
    client = httpx.AsyncClient(transport=PinnedTransport(flip, network))
    with pytest.raises(Blocked):
        asyncio.run(PageReader(client, resolve=flip).read("http://rebind.example/"))
    assert network.hosts == []


def test_every_address_of_a_name_must_be_public() -> None:
    with pytest.raises(Blocked):
        asyncio.run(ensure_public("https://split.example/", _public("211.115.84.70", "10.0.0.5")))


def test_a_pdf_attachment_is_read_as_a_file() -> None:
    """큐넷은 첨부를 octet-stream 으로 주고 이름은 헤더에 담는다(2026-11-06 실측)."""
    from tests.unit.test_files import _pdf

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/octet-stream;;charset=UTF-8",
                "content-disposition": "attachment; filename*=UTF-8''%EC%9D%BC%EC%A0%95.pdf",
            },
            content=_pdf("Exam 2026.09.21"),
        )

    page = asyncio.run(
        PageReader(_client(handler), resolve=_public("211.115.84.70")).read(
            "https://q.example/d?id=1"
        )
    )
    assert page.kind == "pdf" and page.title == "일정.pdf" and "Exam 2026.09.21" in page.text


def test_attachment_links_are_collected_with_the_qnet_button() -> None:
    """공지의 일정은 본문이 아니라 첨부에 있었다.

    큐넷 첨부는 링크가 아니라 자바스크립트 버튼이다.
    """
    from src.brain.web import links_of

    base = "https://www.q-net.or.kr/man004.do?ARTL_SEQ=5276928&BOARD_ID=Q001&gSite=Q&id=man00402"
    html = (
        "<p>2026 일정.pdf</p><button onclick=\"javascript:fileDownMan004('bbs/Q001/Q001_2268020', "
        "'2026 일정.pdf', '2268020')\"><span>다운로드</span></button>"
        '<a href="/files/guide.hwp">안내문</a><a href="javascript:void(0)">x.pdf</a>'
        '<a href="/notice/123">다른 글</a>'
    )
    links = links_of(html, base)

    assert [link.name for link in links] == ["2026 일정.pdf", "안내문"]
    qnet = urlsplit(links[0].url)
    query = dict(parse_qsl(qnet.query))
    assert qnet.path == "/man004.do" and query["id"] == "man00402s01"
    assert query["ARTL_SEQ"] == "5276928" and query["filePath"] == "bbs/Q001/Q001_2268020"
    assert links[1].url == "https://www.q-net.or.kr/files/guide.hwp"


def test_a_big_file_is_parsed_without_stopping_jarvis(monkeypatch: pytest.MonkeyPatch) -> None:
    """파일 분석은 스레드에서 — 큰 PDF 를 읽는 동안에도 듣기·반응 루프가 돈다."""
    import time

    from src.brain import files as files_module

    def slow(data: bytes, kind: str) -> str:
        time.sleep(0.3)
        return "다 읽었다"

    monkeypatch.setattr(files_module, "text_of", slow)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4")

    async def both() -> tuple:
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.04)
                ticks += 1

        reader = PageReader(_client(handler), resolve=_public("211.115.84.70"))
        page, _ = await asyncio.gather(reader.read("https://a.kr/big.pdf"), tick())
        return page, ticks

    page, ticks = asyncio.run(both())
    assert page.text == "다 읽었다" and ticks == 5
