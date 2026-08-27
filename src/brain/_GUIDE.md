# src/brain/ — 폴더 가이드

## 역할

말을 걸지 정하고, 걸기로 했으면 문장을 만든다. 게이트(싼 필터) → 맥락 조립 →
추론(LLM) → 발화 결정.

**이 폴더가 소유하지 않는 것**: 무엇이 이상인지의 감지(→ `triggers`),
실제 발송(→ `channels`), 데이터 조회(→ `storage`).

---

## 핵심 패턴

### 싼 게이트 먼저, 비싼 LLM 나중
```python
if not self.gate.allows(insight, now):
    return None          # LLM 을 부르지 않는다
```
**이유**: 알림 스팸이 이 프로덕트의 주된 실패 방식이다. 순서를 뒤집으면 30분마다
GPU 를 태우고 결국 "말할 필요 없음" 판정을 받는다.

### 맥락 제공자를 늘려 똑똑하게 만든다
**이유**: 더 큰 모델이 아니다. 2026-08-24 같은 모델·같은 프롬프트에서 수집 현황
제공자 하나를 붙였더니 "배터리 최적화를 확인해라"(존재하지 않는 개념)가
"단축어 설정이 깨졌을 가능성이 높다"로 바뀌었다.

---

## 금지사항

### ❌ 모델 응답을 `str()` 로 감싸지 않는다
```python
# ❌ 금지
return str(choice["message"]["content"])

# ✅ 대신
if not isinstance(content, str) or not content.strip():
    return ""
```
**사고 이력**: 2026-08-21. reasoning-parser 가 붙은 서버는 사고 중 `max_tokens` 에
걸리면 `content` 를 **null** 로 준다. `str(None)` 은 `"None"` 이라는 문자열이 되고,
그게 SKIP 검사와 빈 검사를 둘 다 통과해 사용자에게 발송된다.

### ❌ 발송 전에 쿨다운을 기록하지 않는다
**사고 이력**: 발송 실패까지 기억에 남기면 사용자는 메시지를 못 받았는데 자비스는
"아까 말했지" 하고 쿨다운 내내 침묵한다. `confirm_spoken` 은 **성공 후에만** 부른다.

### ❌ 맥락 제공자 하나가 죽었다고 발화를 포기하지 않는다
**사고 이력**: 캘린더 서버가 내려갔다고 건강 알림까지 멈추면 안 된다.
`assemble()` 은 제공자별로 예외를 삼키고 나머지를 모은다.

---

## GC 패턴

```gc
pattern: "str\(\s*content\s*\)"
message: "모델 content 는 null 일 수 있다 — str() 로 감싸면 \"None\" 이 발송된다"
```

```gc
pattern: "from src\.sectors"
message: "brain 은 플랫폼이다 — 섹터를 알면 안 된다"
```

---

## 하네스

```
tests:
  - tests/unit/test_gate.py
  - tests/unit/test_agent.py
  - tests/unit/test_context.py
  - tests/unit/test_collection_status.py
  - tests/unit/test_vllm_client.py
  - tests/unit/test_reasoning_strip.py
```

```bash
python scripts/harness.py src/brain/
```

---

## 모듈 지도

| 파일 | 책임 |
|------|------|
| `client.py` | `Reasoner` 프로토콜 + vLLM 구현. `<think>` 제거, null content 방어 |
| `reflect.py` | 회고 루프. 흔적을 훑어 믿음을 만들고 시들게 한다 |
| `agent.py` | 게이트 → 맥락 조립 → 추론 → 발화 결정 |
| `gate.py` | 싼 필터. severity 임계값 + 트리거별 쿨다운 |
| `memory.py` | `SpeechLog` — 언제 무슨 말을 했는지 |
| `context.py` | `ContextProvider` 프로토콜 + 조립 |
| `providers.py` | 실제 맥락 제공자 (관측 추이·최근 발화·수집 현황) |
| `prompts.py` | 프롬프트 템플릿 + SKIP 파싱 |
