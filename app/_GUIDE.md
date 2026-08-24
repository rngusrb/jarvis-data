# app/ — 폴더 가이드

## 역할

조립 지점. **어떤 섹터를 켤지 고르는 유일한 곳**이고, 플랫폼에 부품을 넘긴다.

**이 폴더가 소유하지 않는 것**: 루프·수신구(→ `src/runtime`), 판단(→ `src/brain`),
파싱(→ 섹터). 여기 로직이 생기면 그건 잘못 온 것이다.

---

## 핵심 패턴

### 섹터를 아는 유일한 파일
```python
from src.sectors.health import METRICS as HEALTH_METRICS
metrics = MetricRegistry().register(HEALTH_METRICS)
```
**이유**: 플랫폼은 섹터를 모른다. 누군가는 골라야 하고, 그 자리가 여기다.
`tests/invariants/test_boundaries.py` 가 이 파일 하나만 예외로 인정한다.

### 감시·맥락을 레지스트리에서 파생시킨다
```python
stale = [StaleDataTrigger(kind=m.kind, label=m.label, ...) for m in metrics.active()]
```
**이유**: 지표를 손으로 나열하면 카드와 어긋난다. 실제로 그렇게 어긋났었다.

---

## 금지사항

### ❌ 로직을 여기 두지 않는다
**사고 이력**: 2026-08-25 이전 `app/` 은 537줄에 루프·수신구·백필·배선 네 가지를 하고
있었다. 셋은 `app/` 의 일이 아니었다.

### ❌ 섹터 목록을 다른 파일에 복사하지 않는다
**사고 이력**: 2026-08-24. `COLLECTORS` 선언과 `StaleDataTrigger` 목록이 따로 살아서
휴식기 심박 전환 때 한쪽만 갱신됐다.

---

## GC 패턴

```gc
pattern: "def (fold|check|consider|send)\("
message: "app/ 은 조립만 한다 — 판단·집계·발송 로직은 플랫폼이나 섹터로"
```

---

## 하네스

> ⚠️ 배선 전용이라 전용 테스트가 없다. 경계는 불변식이 검사한다.

```
tests:
  - tests/invariants/test_boundaries.py
```

```bash
python scripts/harness.py app/
```

---

## 모듈 지도

| 파일 | 책임 |
|------|------|
| `main.py` | 섹터 등록 → 지표 레지스트리 → 트리거·에이전트·채널 조립 → FastAPI |
