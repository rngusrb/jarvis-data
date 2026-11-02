# src/storage/ — 폴더 가이드

## 역할

관측치와 **자비스가 한 말**을 넣고 꺼낸다.
`ObservationSource`·`ObservationCatalog`·`SpeechLog` 구현.

**이 폴더가 소유하지 않는 것**: 무엇을 저장할지의 판단, 값의 의미.
저장소는 관측치가 수면인지 걸음수인지 모른다.

---

## 핵심 패턴

### 멱등 쓰기
```python
PRIMARY KEY (source, kind, at)
INSERT OR REPLACE ...
```
**이유**: 수집 파이프라인은 언제든 재실행될 수 있어야 한다. 단축어가 두 번 울렸든
백필을 다시 돌렸든 같은 날짜의 같은 지표는 한 줄로 남아야 한다.

---

## 금지사항

### ❌ 표기가 섞일 수 있는 시각을 글자로 비교하지 않는다
```python
# ❌ 금지 — t.at.isoformat() 로 쓰고 "WHERE at >= ?" 로 비교   # +09:00 과 +00:00 이 섞이면 틀린다
# ✅ 대신 — 쓸 때 stamp(t.at) 로 UTC 를 맞추거나(흔적·발화·주제 나무)
#           비교할 때 julianday(at) 로 순간을 비교한다(관측치 — 옛 행이 +09:00)
```
**이유**: 시각은 문자열로 저장되고 문자열로 비교된다. 표기가 섞이면 경계가 최대 9시간
어긋나고, "마지막"이 실제로 가장 늦은 행이 아니게 된다 — 발화 기록이면 쿨다운이 일찍 풀린다.
**사고 이력**: 2026-10-29 구조 재검토에서 관측치 쪽을 발견, 10-30 고침. 같은 날 리뷰가 발화
기록도 같은 방식임을 재현했다(한국 시각 한 줄이 섞이자 6시간 쿨다운이 5.5시간에 풀림).
지금 쓰는 쪽은 모두 UTC 라 실제 피해는 없었다.

### ❌ 관측치를 벡터 DB에 넣지 않는다
**사고 이력**: 초기 계획이 Qdrant 였다. 관측치는 시계열 숫자고 필요한 질의가
"최근 8일치"라 벡터 DB가 아주 못하는 일이다. Qdrant 는 텍스트(메모·대화·관심사)용으로 남긴다.

### ❌ 같은 주제를 두 줄로 만들지 않는다
```python
# ❌ 금지 — 같은 자리(부모)에 같은 이름의 살아 있는 주제를 또 만든다
# ✅ 대신 — plan 이 거기 붙인다(create → attach). 이름을 바꿔 겹치면 거절하고 merge 를 쓰게 한다
```
**이유**: "주거"가 두 줄이면 어느 게 지금 생각인지 알 수 없다. 옛 믿음은 kind 를 기본키로 막았다.
나무에선 이름이 아니라 번호로 이으므로(이름은 바뀐다) 검사가 막는다.

### ❌ 지우지 않는다 — 접고, 뗀 때를 적는다
```sql
-- ❌ 금지 — DELETE FROM topics / DELETE FROM topic_evidence
-- ✅ 대신 — topics.retired_at, topic_evidence.removed_at
```
**이유**: 근거는 옮기지 않고 연결만 바꾼다. 가지를 나눌 때 줄기에서 내려간 연결, 합칠 때 옮겨 간
연결이 지워지면 "그때 왜 이렇게 봤나"를 다시 볼 수 없다(BACKLOG 구조 재검토 결정).

### ❌ 잊는 쪽을 빼먹지 않는다
**이유**: 생성만 있고 회수가 없으면 몇 달 뒤 3년 전 관심사가 프롬프트에
끼어든다. 옛 믿음은 `forget_stale()` 이 지웠고, 나무는 오래 근거가 없는 주제를 회고가
프롬프트 전에 접는다(`core.topics.stale`, 이유와 함께 기록).

### ❌ 낡은 나무 위에서 짠 계획을 적용하지 않는다
```python
# ❌ 금지 — load() 뒤 LLM 을 1분 기다리는 동안 다른 회고가 바꾼 나무에 그대로 쓴다
# ✅ 대신 — apply 가 쓰기 잠금을 잡고 변경 기록 끝 번호(version)를 견준다. 다르면 StaleTree
```
**이유**: 계획은 읽은 나무와의 **차이**로 쓰인다. 그새 다른 회고(서버의 매일 회고와 손으로 돌린
CLI)가 같은 주제를 합쳤으면 접힌 주제에 근거를 붙이거나 닫힌 연결을 되살린다.

### ❌ 발화 기억을 메모리에만 두지 않는다
```python
# ❌ 금지 — Gate(log=InMemorySpeechLog()) 를 운영에서
# ✅ 대신 — Gate(log=SQLiteSpeechLog(db_path))
```
**사고 이력**: 쿨다운이 6시간일 땐 메모리로 버텼다. 만성 신호에 7일 쿨다운이
생기면서 무너졌다 — 배포할 때마다 재시작되고, 재시작하면 쿨다운이 풀려서
"요즘 잠이 부족하네요"를 다시 한다. 알림 스팸이 이 프로덕트의 주된 실패 방식이다.

### ❌ 연결을 오래 들고 있지 않는다
```python
# ❌ 금지 — self._conn 을 필드로
# ✅ 대신 — 호출마다 열고 닫는다
```
**사고 이력**: sqlite3 연결이 스레드를 넘나들면 터진다. FastAPI 이벤트 루프와 배치
스크립트가 같은 저장소를 건드린다.

---

## GC 패턴

```gc
pattern: "except Exception:\s*(pass|continue)"
message: "silent failure 금지 — 저장 실패가 조용히 넘어가면 데이터가 사라진 걸 모른다"
```

---

## 하네스

```
tests:
  - tests/unit/test_place_store.py
  - tests/unit/test_event_store.py
  - tests/unit/test_catalog.py
  - tests/unit/test_storage.py
  - tests/unit/test_conversation_store.py
  - tests/unit/test_speech_log_sqlite.py
  - tests/unit/test_trace_store.py
  - tests/unit/test_topic_store.py
  - tests/integration/test_listen_loop.py
```

```bash
python scripts/harness.py src/storage/
```

---

## 모듈 지도

| 파일 | 책임 |
|------|------|
| `sqlite.py` | 관측치 저장소. 멱등 쓰기 + 범위 조회 + 마지막 수신 시각 |
| `speech.py` | 발화 기억. 재시작을 넘겨 쿨다운을 지킨다. 무엇에 대한 판단이었나(`about`)도 남긴다 — 옛 행은 비어 있다 |
| `traces.py` | 흔적 저장소. 중복은 조용히 무시(수집기가 겹쳐 읽는 게 정상). 흔적에 번호(`id`)를 붙여 돌려주고, 조회 끝(`until`)을 받는다(회고 되감기) |
| `conversation.py` | 대화 기록 + 폴링 커서. 삽입 순서로 정렬한다 |
| `topics.py` | 주제 나무 저장소 — 주제·근거 연결·변경 기록·메모·회고 기록. 나무를 바꾸는 길은 `apply` 하나(검사한 나무와의 차이를 한 트랜잭션에, 낡은 나무면 `StaleTree`). 지우지 않고 접고 뗀다 |
| `events.py` | 사건 저장소. (source, kind, at, label) 중복 무시, 시각은 쓸 때 UTC. 받은 때는 처음 받은 때(11-02 에 관측치에서 옮긴 위치 75건은 모름 — 비어 있다. 옛 74건 + 재시작 전 옛 코드가 받은 1건) |
| `catalog.py` | 여러 저장소의 마지막 수신을 한 장으로(`MergedCatalog`). 같은 이름이 두 표에 있으면(옮기는 중) 늦은 쪽을 쓰고 경고를 남긴다 |
| `places.py` | 장소 저장소. 장소 없는 좌표 사건을 붙인다(`assign_new`, 쓰기 잠금 먼저) |
