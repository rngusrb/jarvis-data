"""웹 — 검색 결과 읽기, 내부망 막기, 본문 추리기."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List

import httpx
import pytest

from src.brain.web import Blocked, PageReader, SearxSearch, ensure_public, extract, mobile


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
