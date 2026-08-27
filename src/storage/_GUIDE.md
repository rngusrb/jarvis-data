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

### ❌ 관측치를 벡터 DB에 넣지 않는다
**사고 이력**: 초기 계획이 Qdrant 였다. 관측치는 시계열 숫자고 필요한 질의가
"최근 8일치"라 벡터 DB가 아주 못하는 일이다. Qdrant 는 텍스트(메모·대화·관심사)용으로 남긴다.

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
  - tests/unit/test_storage.py
  - tests/unit/test_speech_log_sqlite.py
```

```bash
python scripts/harness.py src/storage/
```

---

## 모듈 지도

| 파일 | 책임 |
|------|------|
| `sqlite.py` | 관측치 저장소. 멱등 쓰기 + 범위 조회 + 마지막 수신 시각 |
| `speech.py` | 발화 기억. 재시작을 넘겨 쿨다운을 지킨다 |
