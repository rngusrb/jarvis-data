"""브라우저 — 진짜 헤드리스 크롬으로. 시험 사이트는 로컬이고, 크롬은 문(프록시)을 거쳐서만 닿는다.

크롬은 `site.example` 을 공개 주소(211.115.84.70)로 알고 문을 지나 연다 — 문이 그 주소로 붙으려는
것을 로컬 시험 서버로 잇는다(`opener`). 내부 주소(127.0.0.1)는 문이 막는다.
"""

from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, List, Tuple
from urllib.parse import parse_qs, urlsplit

import pytest

from src.brain.browser import Browser, BrowserUnavailable, Refused, render

PUBLIC = "211.115.84.70"

PAGES = {
    "/": """<html><head><title>시험 안내</title></head><body>
<main><h1>2026 시험 안내</h1>
<p>일정은 아래 시험일정 페이지에 있다. 날짜는 탭을 눌러야 보인다.</p></main>
<a href="/schedule">시험일정</a>
<button onclick="document.getElementById('more').innerText = MORE">더보기</button>
<div id="more"></div>
<script>const MORE = '추가 안내: 빈자리 접수 10.18~10.19';</script>
<form action="/search"><input name="kwd" placeholder="검색어 입력"><button>검색</button></form>
<input name="name" placeholder="이름">
<input type="password" name="pw">
<a href="/popup" target="_blank">새 창</a>
<a href="/file.pdf">일정표 파일</a>
<img src="http://127.0.0.1:{port}/secret.png">
<iframe src="/frame" width="400" height="80"></iframe>
</body></html>""",
    "/schedule": """<html><head><title>시험일정</title></head><body><main><div id="x"></div>
</main><script>setTimeout(() => {
  document.getElementById('x').innerText = '3회 실기 접수 9.21~9.28, 시험 10.25';
}, 300)</script></body></html>""",
    "/popup": """<html><head><title>새 창</title></head><body>
<main><p>새 창의 내용이다.</p></main></body></html>""",
    "/frame": """<html><body>
<p>프레임 안의 안내 글이 여기에 꽤 길게 들어 있다. 프레임도 본다.</p>
<a href="/schedule">프레임 링크</a></body></html>""",
    "/alert": """<html><head><title>알림</title></head><body><script>alert('잠깐')</script>
<main><p>알림 뒤의 본문.</p></main></body></html>""",
    "/sneaky": """<html><head><title>몰래</title></head><body>
<main><p>몰래 내려받는 페이지.</p></main>
<iframe src="/file.pdf?n=1" style="display:none"></iframe>
<iframe src="/file.pdf?n=2" style="display:none"></iframe>
<script src="/slow.js"></script>
<script>setTimeout(() => {
  for (let i = 0; i < 3; i++) window.open('/popup?n=' + i);
}, 200);</script>
</body></html>""",
    "/trap": """<html><head><title>함정</title></head><body><main><p>떠나지 못하게 하는 페이지.</p>
<a href="/popup">나가기</a></main>
<script>addEventListener('beforeunload', (e) => {
  e.preventDefault(); e.returnValue = '';
});</script>
</body></html>""",
    "/select": """<html><head><title>고르기</title></head><body><main><p id="out">고르기 전</p>
<select name="year" onchange="document.getElementById('out').innerText = this.value + '년 일정'">
<option>2025</option><option>2026</option></select></main></body></html>""",
    "/spin": """<html><head><title>바쁨</title></head><body><main><p>곧 바빠지는 페이지.</p></main>
<script>setTimeout(() => { while (true) {} }, 300);</script></body></html>""",
    "/tabs": """<html><head><title>연간 일정</title></head><body><main>
<ul><li id="pro" style="cursor:pointer">기술사</li>
<li id="eng" style="cursor:pointer">기사</li></ul>
<form><div id="table">기술사 일정 표</div></form></main>
<script>
document.getElementById('eng').addEventListener('click', () => {
  setTimeout(() => {
    document.getElementById('table').innerText = '기사 3회 실기 접수 9.21~9.28';
  }, 800);
});
</script></body></html>""",
}


class _Site(BaseHTTPRequestHandler):
    seen: List[str] = []
    port = 0

    def do_GET(self) -> None:  # noqa: N802 — http.server 의 이름
        type(self).seen.append(self.path)
        path = urlsplit(self.path).path
        if path == "/search":
            words = parse_qs(urlsplit(self.path).query).get("kwd", [""])[0]
            self._send("text/html; charset=utf-8", f"<main><p>검색 결과: {words}</p></main>")
        elif path == "/slow.js":
            # 문서 읽기를 1초 붙잡는다 — 그 사이 숨은 프레임의 내려받기가 열기(goto) 안에서
            # 일어난다.
            time.sleep(1.0)
            self._send("text/javascript", "")
        elif path == "/rtc":
            udp = parse_qs(urlsplit(self.path).query).get("udp", ["9"])[0]
            script = (
                "const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:127.0.0.1:"
                + udp
                + "'}]});"
                "pc.createDataChannel('x'); pc.createOffer().then(o => pc.setLocalDescription(o));"
            )
            self._send(
                "text/html; charset=utf-8", f"<main><p>rtc</p></main><script>{script}</script>"
            )
        elif path == "/file.pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="plan.pdf"')
            self.end_headers()
            self.wfile.write(b"%PDF-1.4 ...")
        elif path in PAGES:
            self._send("text/html; charset=utf-8", PAGES[path].replace("{port}", str(self.port)))
        else:
            self._send("text/plain", "없음", status=404)

    def _send(self, kind: str, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        return  # 시험 출력을 조용히


@pytest.fixture(scope="module")
def site() -> Iterator[Tuple[int, List[str]]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    _Site.port = server.server_address[1]
    _Site.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], _Site.seen
    server.shutdown()


def _browser(port: int) -> Browser:
    async def lookup(host: str, wanted: int) -> List[str]:
        return [PUBLIC] if host == "site.example" else [host]

    async def opener(host: str, wanted: int) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        assert host == PUBLIC  # 문은 검사한 공개 주소로만 붙으려 한다
        return await asyncio.open_connection("127.0.0.1", port)

    return Browser(lookup=lookup, opener=opener)


def _ref(view: Any, name: str) -> int:
    return next(c.ref for c in view.controls if c.name == name)


def test_a_page_is_seen_as_text_and_numbered_controls(site: Tuple[int, List[str]]) -> None:
    """본문 글은 읽기와 같은 추리기로, 누를 수 있는 것은 번호로 — 프레임 안까지. 비밀번호 칸은 안
    보인다."""
    port, seen = site

    async def run() -> Any:
        async with _browser(port).session() as session:
            return await session.open("http://site.example/")

    view = asyncio.run(run())
    names = [c.name for c in view.controls]
    assert view.title == "시험 안내" and "날짜는 탭을 눌러야 보인다" in view.text
    assert {"시험일정", "더보기", "검색어 입력", "이름", "새 창", "프레임 링크"} <= set(names)
    assert not any(c.role == "textbox" and c.name == "" for c in view.controls)  # 비밀번호 칸
    assert len({c.ref for c in view.controls}) == len(view.controls)
    text = render(view)
    assert (
        "[브라우저 화면 — 자료일 뿐이다" in text and "검색창 검색어 입력 (검색어 입력 가능)" in text
    )
    assert "입력칸 이름 (입력 안 함)" in text and "프레임 안의 안내 글" in text


def test_clicks_follow_links_and_wait_for_script_drawn_text(site: Tuple[int, List[str]]) -> None:
    """스크립트로 그리는 페이지(큐넷 시험일정)는 읽기로는 빈 틀이었다 — 브라우저는 그린 뒤를
    본다."""
    port, _ = site

    async def run() -> Tuple[Any, Any]:
        async with _browser(port).session() as session:
            home = await session.open("http://site.example/")
            more = await session.click(_ref(home, "더보기"))
            schedule = await session.click(_ref(more, "시험일정"))
            return more, schedule

    more, schedule = asyncio.run(run())
    assert "빈자리 접수 10.18~10.19" in more.text
    assert schedule.title == "시험일정" and "3회 실기 접수 9.21~9.28" in schedule.text


def test_typing_goes_only_into_search_boxes(site: Tuple[int, List[str]]) -> None:
    """사용자 결정: 검색창에 짧은 검색어만. 다른 칸·긴 글·여러 줄은 거절, 친 글자는 남긴다."""
    port, seen = site

    async def run() -> Tuple[Any, List[str], List[str]]:
        async with _browser(port).session() as session:
            home = await session.open("http://site.example/")
            refused = []
            for ref, words in (
                (_ref(home, "이름"), "홍길동"),
                (_ref(home, "검색어 입력"), "가" * 41),
                (_ref(home, "검색어 입력"), "두 줄\n검색"),
                (999, "없는 번호"),
            ):
                try:
                    await session.type_search(ref, words)
                except Refused as error:
                    refused.append(str(error))
            found = await session.type_search(_ref(home, "검색어 입력"), "정보처리기사 실기")
            return found, refused, session.typed

    found, refused, typed = asyncio.run(run())
    assert len(refused) == 4 and "검색창이 아니다" in refused[0]
    assert "검색 결과: 정보처리기사 실기" in found.text and typed == ["정보처리기사 실기"]
    assert not any("%ED%99%8D" in path for path in seen)  # "홍"은 어디로도 가지 않았다


def test_inside_addresses_never_load(site: Tuple[int, List[str]]) -> None:
    """페이지가 내부 주소(127.0.0.1)를 불러도, 직접 열려 해도 문이 막는다."""
    port, seen = site

    async def run() -> Tuple[List[str], str]:
        async with _browser(port).session() as session:
            await session.open("http://site.example/")
            try:
                await session.open(f"http://127.0.0.1:{port}/secret-page")
                outcome = "열림"
            except Refused as error:
                outcome = str(error)
            view = await session.view()
            return session.proxy.blocked, outcome + view.text

    blocked, outcome = asyncio.run(run())
    assert "127.0.0.1" in blocked
    assert not any("secret" in path for path in seen)
    assert "secret" not in outcome or "하지 못했다" in outcome


def test_files_are_not_downloaded_only_named(site: Tuple[int, List[str]]) -> None:
    """파일은 받지 않고 주소만 — 읽는 쪽(별도 프로세스)이 따로 읽는다."""
    port, _ = site

    async def run() -> Tuple[Any, List[Any]]:
        async with _browser(port).session() as session:
            home = await session.open("http://site.example/")
            after = await session.click(_ref(home, "일정표 파일"))
            return after, session.files

    after, files = asyncio.run(run())
    assert [f.url for f in files] == ["http://site.example/file.pdf"]
    assert files[0].name == "plan.pdf" and "파일이라 화면에 안 열렸다" in after.note


def test_alerts_popups_and_screenshots(site: Tuple[int, List[str]]) -> None:
    """알림창은 닫고 넘어간다, 새 창은 그쪽을 본다, 화면은 JPEG 로 찍는다."""
    port, _ = site

    async def run() -> Tuple[Any, Any, bytes]:
        async with _browser(port).session() as session:
            alerted = await session.open("http://site.example/alert")
            home = await session.open("http://site.example/")
            popup = await session.click(_ref(home, "새 창"))
            shot = await session.screenshot()
            return alerted, popup, shot

    alerted, popup, shot = asyncio.run(run())
    assert "알림 뒤의 본문" in alerted.text
    assert popup.title == "새 창" and "새 창의 내용" in popup.text
    assert shot[:2] == b"\xff\xd8" and len(shot) > 1000


def test_only_web_addresses_are_opened(site: Tuple[int, List[str]]) -> None:
    port, _ = site

    async def run() -> List[str]:
        refused = []
        async with _browser(port).session() as session:
            for url in ("file:///etc/passwd", "chrome://settings", "javascript:alert(1)"):
                try:
                    await session.open(url)
                except Refused as error:
                    refused.append(str(error))
        return refused

    assert len(asyncio.run(run())) == 3


def test_a_browser_that_cannot_start_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """보안 격리가 막혀 못 뜨면 시끄럽게 — 격리를 끄고 몰래 돌지 않는다."""
    import playwright.async_api

    class Chromium:
        async def launch(self, **options: Any) -> Any:
            assert options["chromium_sandbox"] is True
            raise RuntimeError("Chromium sandboxing failed! No usable sandbox!")

    class Started:
        chromium = Chromium()

        async def stop(self) -> None:
            return None

    class Starter:
        async def start(self) -> Started:
            return Started()

    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: Starter())

    async def run() -> None:
        async with Browser().session():
            raise AssertionError("뜨면 안 된다")

    with pytest.raises(BrowserUnavailable, match="AppArmor"):
        asyncio.run(run())


def test_script_bound_tabs_are_numbered_and_late_changes_are_waited_for(
    site: Tuple[int, List[str]],
) -> None:
    """onclick 없이 스크립트로 단 탭(손가락 커서)도 누를 것이다. 누른 뒤 0.8초에 채워지는 표도 본다
    — 로드만 기다렸더니 바뀌기 전 화면을 봤다(큐넷 연간 시험일정, 2026-11-06 실측)."""
    port, _ = site

    async def run() -> Tuple[Any, Any]:
        async with _browser(port).session() as session:
            tabs = await session.open("http://site.example/tabs")
            after = await session.click(_ref(tabs, "기사"))
            return tabs, after

    tabs, after = asyncio.run(run())
    assert {"기술사", "기사"} <= {c.name for c in tabs.controls if c.role == "clickable"}
    assert "기술사 일정 표" in tabs.text  # form 안의 본문도 보인다
    assert "기사 3회 실기 접수 9.21~9.28" in after.text


def test_the_browser_starts_with_the_guards_on(
    site: Tuple[int, List[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """띄우는 설정을 붙든다 — QUIC·WebRTC UDP·DNS 미리 풀기 끔, 격리 켬, 루프백도 프록시로, 비밀값
    없는 환경, 내려받기 안 받음, 서비스 워커 막음. 한꺼번에 지워도 시험이 다 통과했다(JAR-036
    리뷰)."""
    from playwright.async_api._generated import Browser as PlaywrightBrowser
    from playwright.async_api._generated import BrowserType

    from src.brain.browser import ARGS

    port, _ = site
    seen: dict = {}
    launch, new_context = BrowserType.launch, PlaywrightBrowser.new_context

    async def spy_launch(self: Any, **options: Any) -> Any:
        seen["launch"] = options
        return await launch(self, **options)

    async def spy_context(self: Any, **options: Any) -> Any:
        seen["context"] = options
        return await new_context(self, **options)

    monkeypatch.setattr(BrowserType, "launch", spy_launch)
    monkeypatch.setattr(PlaywrightBrowser, "new_context", spy_context)
    monkeypatch.setenv("JARVIS_TELEGRAM_BOT_TOKEN", "secret-token")

    async def run() -> None:
        async with _browser(port).session() as session:
            await session.open("http://site.example/")

    asyncio.run(run())
    launched, context = seen["launch"], seen["context"]
    assert set(ARGS) <= set(launched["args"]) and launched["chromium_sandbox"] is True
    assert {"--disable-quic", "--dns-prefetch-disable"} <= set(ARGS)
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in ARGS
    assert launched["proxy"]["bypass"] == "<-loopback>"
    assert set(launched["env"]) == {"PATH", "HOME", "LANG"}
    assert context["accept_downloads"] is False and context["service_workers"] == "block"


def test_webrtc_sends_no_udp_around_the_door(site: Tuple[int, List[str]]) -> None:
    """WebRTC 는 문(프록시)을 안 거치는 UDP 로 나간다 — 설정을 빼면 STUN 패킷 약 130개가
    127.0.0.1·LAN 으로 나갔다(JAR-036 리뷰 대조 실험). 켠 채로는 하나도 안 나간다."""
    import socket

    port, _ = site
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(0.2)
    udp = listener.getsockname()[1]

    async def run() -> None:
        async with _browser(port).session() as session:
            await session.open(f"http://site.example/rtc?udp={udp}")
            await asyncio.sleep(2.0)

    asyncio.run(run())
    got = 0
    try:
        while True:
            listener.recvfrom(2048)
            got += 1
    except socket.timeout:
        pass
    finally:
        listener.close()
    assert got == 0


def test_downloads_and_windows_the_hand_did_not_ask_for_are_dropped(
    site: Tuple[int, List[str]],
) -> None:
    """숨은 프레임이 흘린 내려받기 수백 개가 첨부 상한을 우회해 허용 목록·로그·메모리를 채웠고, 아무
    조작 없이 연 창 60개가 손의 화면을 넘겼다(JAR-036 리뷰 재현). 손이 누른 동작의 것만 받는다."""
    port, _ = site

    async def run() -> Tuple[Any, Any, Any]:
        async with _browser(port).session() as session:
            first = session.page
            view = await session.open("http://site.example/sneaky")
            await asyncio.sleep(1.0)
            return session, first, view

    session, first, view = asyncio.run(run())
    assert session.files == [] and session.page is first and view.title == "몰래"
    assert session.ignored >= 5  # 내려받기 둘 + 창 셋


def test_a_dead_tab_is_said_not_shown_as_an_empty_page(
    site: Tuple[int, List[str]], caplog: pytest.LogCaptureFixture
) -> None:
    """탭이 죽으면 손은 "(본문 글이 없다)" 화면을 받고 로그는 debug 뿐이었다(JAR-036 리뷰)."""
    port, _ = site

    async def run() -> str:
        async with _browser(port).session() as session:
            await session.open("http://site.example/")
            cdp = await session.context.new_cdp_session(session.page)
            try:
                await asyncio.wait_for(cdp.send("Page.crash"), timeout=2.0)
            except Exception:
                pass  # 죽이는 명령은 답이 안 온다(탭이 죽는다)
            await asyncio.sleep(0.5)
            try:
                await session.view()
            except Refused as error:
                return str(error)
            return "보였다"

    assert "페이지가 죽었다" in asyncio.run(run())
    assert "브라우저 탭이 죽었다" in caplog.text


def test_a_page_that_begs_to_stay_does_not_trap_the_hand(site: Tuple[int, List[str]]) -> None:
    """ "이 페이지를 떠날까요"에 머물기로 답했더니 떠나는 누름이 안 먹고 뒤로 가기가 갇혔다(리뷰
    재현)."""
    port, _ = site

    async def run() -> Any:
        async with _browser(port).session() as session:
            trap = await session.open("http://site.example/trap")
            return await session.click(_ref(trap, "나가기"))

    assert asyncio.run(run()).title == "새 창"


def test_choose_picks_only_a_visible_option(site: Tuple[int, List[str]]) -> None:
    port, _ = site

    async def run() -> Tuple[Any, Any, str]:
        async with _browser(port).session() as session:
            page = await session.open("http://site.example/select")
            year = next(c for c in page.controls if c.role == "select")
            try:
                await session.choose(year.ref, "1999")
                refused = ""
            except Refused as error:
                refused = str(error)
            return page, await session.choose(year.ref, "2026"), refused

    page, chosen, refused = asyncio.run(run())
    assert "항목: 2025 / 2026" in render(page) and "2026년 일정" in chosen.text
    assert "보이는 항목 중에서" in refused


def test_a_page_that_stops_answering_is_given_up_on(
    site: Tuple[int, List[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """메인 스레드를 붙잡는 페이지에서 화면 보기가 끝없이 기다렸다(JAR-036 리뷰) — 시간 상한."""
    from src.brain import browser

    monkeypatch.setattr(browser, "ASK", 1.0)
    port, _ = site

    async def run() -> str:
        async with _browser(port).session() as session:
            try:
                await session.open("http://site.example/spin")
                await asyncio.sleep(0.5)
                await session.view()
            except Refused as error:
                return str(error)
            return "보였다"

    assert "답하지 않는다" in asyncio.run(asyncio.wait_for(run(), timeout=60))


def test_the_chrome_folder_must_not_be_ours_to_change(tmp_path: Any) -> None:
    """격리 허용 프로필이 붙는 폴더를 이 사용자가 고칠 수 있으면 띄우지 않는다(JAR-036 리뷰)."""
    from src.brain.browser import _safe_folder

    with pytest.raises(BrowserUnavailable, match="고칠 수 있다"):
        _safe_folder(str(tmp_path))
    with pytest.raises(BrowserUnavailable, match="없다"):
        _safe_folder(str(tmp_path / "없음"))
    _safe_folder("/usr")  # root 가 가진 폴더
