# src/runtime/ — 폴더 가이드

## 역할

돌아가는 기계 두 개. 데이터가 들어오는 문(`ingest`)과 주기적으로 판단하는
루프(`loop`).

**이 폴더가 소유하지 않는 것**: 어떤 섹터를 켤지(→ `app/main.py`),
접는 법의 선택(→ 지표 카드), 판단(→ `brain`).

---

## 핵심 패턴

### 루프는 조립만 한다
```python
window = source.recent(...); insight = trigger.check(window, now)
message = await agent.consider(insight, now); await channel.send(message)
```
**이유**: 판단 로직이 여기 들어오면 채널이나 트리거를 갈아끼울 때 루프도 고쳐야 한다.

### 수집 직후에 한 주기를 앞당긴다
**이유**: 주기 루프만 있으면 아침에 데이터가 도착해도 최대 30분을 기다린다.
어젯밤 이야기는 기상 직후에 해야 쓸모가 있다. 타이머는 그대로 둔다 —
데이터가 **안** 들어오는 것도 신호이고, 그걸 알아채려면 시계가 돌아야 한다.

---

## 금지사항

### ❌ 지표 이름을 이 폴더에 박지 않는다
```python
# ❌ 금지
SUMMED_KINDS = {"step_count", ...}

# ✅ 대신 — 카드에서 읽는다
metric = registry.get(kind); FOLDERS[metric.fold](...)
```
**사고 이력**: 2026-08-24. 수신구가 `SLEEP_KIND`·`SUMMED_KINDS`·`AVERAGED_KINDS` 를
들고 있어서 섹터가 늘 때마다 이 파일이 자랐다.

### ❌ 접는 계산을 여기서 다시 구현하지 않는다
**사고 이력**: 2026-08-24. 폰이 합산해 보내니 조각 수가 사라져 품질 필터가 무력화됐고,
걸음수는 기기 중복으로 9만이 나왔다. 계산이 두 군데 살면 경로에 따라 값이 달라지고,
원인이 데이터가 아니라 **코드 위치**라 추적이 지독히 어렵다.

### ❌ 주기 실행과 수집 훅이 겹쳐 돌게 두지 않는다
**사고 이력**: 게이트는 발송에 **성공한 뒤에야** 쿨다운을 기록한다. 나란히 달리는 두
주기는 둘 다 게이트를 통과해 같은 말을 두 번 한다. `JarvisLoop` 이 잠금을 들고 있다.

---

## GC 패턴

```gc
pattern: "\"(sleep_hours|step_count|resting_heart_rate|heart_rate_avg)\""
message: "수신구·루프에 지표 이름을 박지 않는다 — 카드에서 읽는다"
```

```gc
pattern: "from src\.sectors"
message: "runtime 은 플랫폼이다 — 섹터를 알면 안 된다"
```

---

## 하네스

```
tests:
  - tests/integration/test_loop.py
  - tests/integration/test_ingest.py
  - tests/integration/test_ingest_spans.py
  - tests/integration/test_ingest_samples.py
```

```bash
python scripts/harness.py src/runtime/
```

---

## 모듈 지도

| 파일 | 책임 |
|------|------|
| `loop.py` | 자비스 루프. 주기 실행 + 수집 훅, 중복 발화 잠금 |
| `ingest.py` | 수신구 세 개(`/ingest`, `/ingest/spans`, `/ingest/samples`) + `/health` |
