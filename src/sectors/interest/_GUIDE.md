# src/sectors/interest/ — 폴더 가이드

## 역할

"이 사람이 지금 무엇에 관심이 있나"를 맡는다. 브라우저 기록·파일·스크린샷처럼
**의도가 드러나는 흔적**이 여기 모인다.

**이 폴더가 소유하지 않는 것**: 흔적을 믿음으로 바꾸는 계산. 그건 플랫폼
(`src/brain/reflect.py`)이 한다. 이 폴더는 **어떤 흔적이 존재하는지만** 선언한다.

---

## 핵심 패턴

### 흔적의 주인은 목적이지 기계가 아니다
```python
WEB_VISIT = TraceKind(kind="web_visit", collector="mac_chrome", sensitive=True)
```
**이유**: 검색 기록은 쇼핑에도 스케줄에도 쓰인다. 소스마다 섹터를 만들면
(`browser` 섹터, `photos` 섹터) 나중에 "이 흔적은 누구 것이냐"로 싸운다.
**흔적의 주인은 그걸 만든 기계가 아니라 그걸 쓰는 목적**이다.

### 지표도 트리거도 없는 섹터가 가능하다
```python
METRICS: list = []
TRIGGERS: list = []
```
**이유**: 흔적은 하나로 말할 거리가 안 된다. "전세대출을 검색했다"고 알림을
보내는 건 이상하다. 회고가 주기적으로 훑어 믿음으로 만들고, 발화는 그 믿음에서
나온다. 빈 목록을 내보내야 `app/main.py` 가 모든 섹터를 같은 모양으로 순회한다.

---

## 금지사항

### ❌ 흔적에 트리거를 달지 않는다
```python
# ❌ 금지 — "전세대출 검색됨!" 하고 즉시 알림
# ✅ 대신 — 회고가 모아서 "요즘 이사를 알아보네"
```
**이유**: 흔적 하나는 신호가 아니다. 서른 개가 모여야 의미가 된다. 즉시 반응하면
사용자가 검색할 때마다 말을 거는 비서가 되고, 그게 이 프로덕트가 죽는 방식이다.

### ❌ 민감한 흔적을 sensitive 표시 없이 등록하지 않는다
```python
# ❌ 금지 — TraceKind(kind="web_visit", ...)
# ✅ 대신 — TraceKind(kind="web_visit", ..., sensitive=True)
```
**이유**: 검색 기록에는 본인도 자비스가 언급 안 했으면 하는 게 섞인다. 수집을
좁히는 게 답은 아니다 — 안 모은 데이터는 나중에 소급이 안 된다. 대신 프롬프트에
넣을지를 따로 정한다. 이건 프라이버시 장치이면서 품질 장치이기도 하다.

---

## GC 패턴

```gc
pattern: "TraceKind\\((?![^)]*collector)"
message: "흔적 카드에 collector 를 적는다 — 누가 보내는지 모르면 수집이 멈춘 걸 알 수 없다"
```

---

## 하네스

```
tests:
  - tests/integration/test_ingest_traces.py
  - tests/unit/test_trace_store.py
```

```bash
python scripts/harness.py src/sectors/interest/
```

---

## 모듈 지도

| 파일 | 책임 |
|------|------|
| `traces.py` | 흔적 카드. 지금은 `web_visit` 하나 |
