# src/triggers/ — 폴더 가이드

## 역할

관측치를 보고 "말할 거리가 있나"를 판단한다. 판단 결과는 `Insight`이고, 그게
실제로 발화될지는 여기서 정하지 않는다.

**이 폴더가 소유하지 않는가**: 발화 여부(→ `brain/gate`), 문장 만들기(→ `brain/agent`),
데이터 가져오기(→ `storage`).

---

## 핵심 패턴

### 순수 함수
```python
def check(self, window: Sequence[Observation], now: datetime) -> Optional[Insight]: ...
```
**이유**: DB도 LLM도 네트워크도 건드리지 않는다. 그래야 테스트가 싸고, 과거 데이터로
백테스트를 돌려 "왜 자비스가 이 말을 했는가"를 재현할 수 있다.

### `now`를 인자로 받는다
**이유**: 내부에서 `datetime.now()` 를 부르면 순수성이 깨져 백테스트가 불가능해진다.
수면 트리거의 창 크기를 8일→21일로 바꿀 때 이 백테스트가 판단 근거였다.

---

## 금지사항

### ❌ 절대 기준으로 이상을 판정하지 않는다
```python
# ❌ 금지
if sleep_hours < 7: alert()

# ✅ 대신 — 개인 baseline 대비 편차
if avg - latest >= drop_hours: ...
```
**사고 이력**: 2026-08-21 백테스트. 이 사용자 평균 수면은 4.92시간이라 "7시간 미만"
기준이면 **83일 중 73일(88%)** 에 알림이 울린다. 첫 주에 알림이 꺼진다.

### ❌ 창 크기를 "며칠"로 고정하지 않는다
**사고 이력**: 2026-08-24. 창이 8일이면 "매일 기록이 있다"를 전제한다. 실제 커버리지는
70%에 최장 6일 공백이라 창에 기록이 3건도 안 모여 판단을 포기하는 밤이 생겼다 —
실제로 2.24시간 잔 밤을 놓쳤다. baseline 은 "최근 N일"이 아니라 **"최근 기록 N개"** 다.

### ❌ 측정 실패를 값으로만 걸러내지 않는다
**사고 이력**: 2026-08-21. `0.18시간(조각 1개)` 은 통계로 잡히지만 `2.82시간(조각 1개)` 은
정상값으로 통과한다. 값이 특이한지가 아니라 **측정이 믿을 만한지**를 봐야 한다.

---

## GC 패턴

```gc
pattern: "datetime\.now\(\)"
message: "트리거는 시계를 읽지 않는다 — now 를 인자로 받아야 백테스트가 가능하다"
```

```gc
pattern: "(import\s+(httpx|requests|sqlite3)|from\s+(httpx|requests|sqlite3))"
message: "트리거는 순수 함수다 — 네트워크·DB 금지"
```

---

## 하네스

```
tests:
  - tests/unit/test_sleep_trigger.py
  - tests/unit/test_stale_trigger.py
```

```bash
python scripts/harness.py src/triggers/
```

---

## 모듈 지도

| 파일 | 책임 |
|------|------|
| `base.py` | `Trigger` 프로토콜 |
| `sleep.py` | 평소 대비 수면 급감. 측정 품질(조각 수) 필터 포함 |
| `stale.py` | 수집이 멈춘 것 자체를 신호로 다룬다 |
