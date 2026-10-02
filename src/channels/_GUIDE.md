# src/channels/ — 폴더 가이드

## 역할

사용자에게 메시지를 밀어넣는다. 그게 전부다.

**이 폴더가 소유하지 않는 것**: 무엇을 말할지, 말할지 말지. 채널은 문장을 받아 보내기만 한다.

---

## 핵심 패턴

### `send()` 하나짜리 프로토콜
```python
class Channel(Protocol):
    name: str
    async def send(self, text: str) -> None: ...
```
**이유**: 지금은 텔레그램이지만 iOS 푸시로 갈아탈 예정이고, 그때 위층
(`runtime/loop.py`)은 한 줄도 바뀌면 안 된다.

### 프레임워크를 붙이지 않는다
**이유**: 텔레그램 Bot API 는 HTTP POST 하나라 `httpx` 로 충분하다.
`python-telegram-bot` 을 붙이면 그 라이브러리의 색깔이 코드에 묻어 나중에 걷어내기 번거롭다.

---

## 금지사항

### ❌ 보낸 사람을 확인하지 않고 받지 않는다
```python
# ❌ 금지 — update 에서 text 만 꺼내 쓴다
# ✅ 대신 — chat.id 가 주인인지 채널이 직접 거른다
```
**이유**: 봇 사용자명(`@koo_jarvis_bot`)은 공개라 **아무나 말을 걸 수 있다.**
거르지 않으면 남이 주인의 수면·위치를 물어볼 수 있다. 위쪽 레이어에 맡기면
언젠가 빠뜨리므로 채널 안에서 막는다.

### ❌ 웹훅을 쓰지 않는다
**이유**: 웹훅은 공개 HTTPS 주소가 필요하다. 서버가 Tailscale 안에만 있고,
개인 건강 데이터 서버를 인터넷에 노출하는 건 이 프로젝트의 전제를 깨는
일이다. long polling 은 나가서 묻는 거라 뚫을 게 없다.

### ❌ Channel 에 receive() 를 끼워넣지 않는다
**이유**: 모든 채널이 양방향일 필요가 없다. 콘솔은 받을 게 없고 iOS 푸시도
받는 경로가 완전히 다르다. 끼워넣으면 그런 채널들이 쓰지도 않을 메서드를
구현하게 된다. 듣는 능력은 `Listener` 로 따로 뒀다.

### ❌ 토큰이 든 URL 을 로깅하지 않는다
```python
url = f"{API_ROOT}/bot{self._bot_token}/sendMessage"   # 이 URL 은 절대 로그에 찍지 않는다
```
**사고 이력**: 텔레그램은 봇 토큰을 URL 경로에 넣는다. 요청 URL 을 습관적으로 로깅하면
토큰이 로그 파일에 남는다.

---

## GC 패턴

```gc
pattern: "logger\.\w+\([^)]*bot_token"
message: "봇 토큰이 URL 에 들어간다 — 로그에 찍으면 안 된다"
```

---

## 하네스

> ⚠️ 전용 테스트가 아직 없다. `runtime/loop` 통합 테스트가 `RecordingChannel` 로
> 간접 검증할 뿐이다. BACKLOG 에 올려둠.

```
tests:
  - tests/unit/test_telegram_receive.py
  - tests/integration/test_loop.py
```

```bash
python scripts/harness.py src/channels/
```

---

## 모듈 지도

| 파일 | 책임 |
|------|------|
| `base.py` | `Channel` 프로토콜 — 텔레그램↔iOS 갈아끼우는 이음매 |
| `console.py` | 개발용. 채널 미설정 시 기본값 |
| `telegram.py` | Bot API `sendMessage` |
