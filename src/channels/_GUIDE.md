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
