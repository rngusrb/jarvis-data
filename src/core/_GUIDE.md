# src/core/ — 폴더 가이드

## 역할

전 레이어가 공유하는 도메인 모델과, 도메인 지식이 없는 계산.
`Observation`·`Insight`·`Metric` 같은 타입과 하루치로 접는 산수가 여기 산다.

**이 폴더가 소유하지 않는 것**: 특정 섹터의 지식. 애플이든 가민이든 알면 안 된다.
저장 방법(→ `storage`), 판단(→ `brain`), 수집(→ `runtime`)도 아니다.

---

## 핵심 패턴

### 출처를 인자로 받는다
```python
# ✅ 이렇게
def daily_sum(samples, kind: str, source: str) -> List[Observation]: ...
```
**이유**: `SOURCE = "apple_health"` 를 상수로 두면 플랫폼이 애플을 아는 것이다.
가민을 붙이는 날 이 파일을 열게 된다.

### frozen dataclass
```python
@dataclass(frozen=True)
class Observation: ...
```
**이유**: 파이프라인을 흘러가는 동안 누가 값을 바꾸지 못하게. 수집→감지→판단→발신을
지나면서 어디서 변조됐는지 추적하는 일은 피하고 싶다.

---

## 금지사항

### ❌ 섹터를 import 하지 않는다
```python
# ❌ 금지
from src.sectors.health import METRICS

# ✅ 대신 — 필요한 것을 인자로 받는다
def fold(samples, kind: str, source: str): ...
```
**사고 이력**: 2026-08-25 구조 개편 전, `parsers/health.py` 가 접는 계산과 애플 XML
파싱을 함께 갖고 있어서 둘을 떼어낼 수 없었다. 플랫폼이 도메인을 알면 섹터를 늘릴 때마다
플랫폼도 고쳐야 한다.

### ❌ 지표 이름을 상수로 박지 않는다
```python
# ❌ 금지
SLEEP_KIND = "sleep_hours"
```
**사고 이력**: 2026-08-24 `sleep_hours` 하나를 네 파일이 조금씩 알고 있었다. 휴식기
심박으로 갈아탈 때 접는 법은 등록했는데 수집 선언을 빠뜨려, 자비스가 일부러 버린 지표를
되살리라고 조를 뻔했다.

---

## GC 패턴

```gc
pattern: "from src\.sectors"
message: "플랫폼은 섹터를 import 하지 않는다 — 방향은 섹터→플랫폼 하나뿐"
```

```gc
pattern: "except Exception:\s*(pass|continue)"
message: "silent failure 금지 — 조용한 성공보다 시끄러운 실패"
```

---

## 하네스

```
tests:
  - tests/unit/test_metric_registry.py
  - tests/unit/test_folding.py
  - tests/invariants/test_boundaries.py
```

```bash
python scripts/harness.py src/core/
```

---

## 금지사항 (추가)

### ❌ 근거 없는 믿음을 만들지 않는다
```python
# ❌ 금지 — evidence 없이 Belief 생성
# ✅ 대신 — 흔적 텍스트를 그대로 인용해 담는다
```
**사고 이력**: 2026-08-26. 회고 실험에서 모델이 7일 평균 두 개를 비교해놓고
"상관관계가 뚜렷하게 나타난다"고 썼다. **근거를 실제보다 세게 말하는 게 이
방식의 주된 실패 모양이다.** 그래서 저장소가 아니라 `Belief.__post_init__`
에서 막는다 — 저장 직전에 막으면 이미 프롬프트에 들어간 뒤다.

### ❌ 흔적을 접으려 하지 않는다
관측치에는 `Fold`(SUM/MEAN)가 있지만 흔적에는 없다. "전세자금대출 금리"를
일곱 개 모아 평균 낼 수 없다. 흔적은 쌓이고, 회고가 요약한다.

---

## 모듈 지도

| 파일 | 책임 |
|------|------|
| `traces.py` | 흔적 카드 + 레지스트리. 지표 카드의 텍스트판 |
| `beliefs.py` | 믿음 모델 + 수명주기(후보→확정→시듦→망각) |
| `models.py` | `Observation`·`Insight`·`Severity` + 저장소 프로토콜 |
| `metrics.py` | 지표 카드(`Metric`)와 레지스트리 |
| `folding.py` | 원본 → 하루치 관측치. 겹침 병합·기기 중복 제거 |
| `config.py` | 환경변수 기반 설정. 비밀값은 `.env` 로만 |
