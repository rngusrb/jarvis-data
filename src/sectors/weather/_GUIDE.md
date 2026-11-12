# src/sectors/weather/ — 폴더 가이드

## 역할

바깥의 사실을 소유한다 — 집 근처 날씨 예보(기상청 단기예보)와 공휴일(특일 정보). 둘 다 공공데이터포털 키
하나로 서버가 직접 가져온다(push 가 아니라 pull — 언제 돌릴지는 `src/runtime/pull.py`).

**이 폴더가 소유하지 않는 것**: "집이 어디인가"(→ 이동 섹터, 조립이 넘겨준다), "그날 쉬나"의 판단
(→ `core/days`), 공휴일의 저장(→ `storage/days`), 말할지 판단(→ `brain`).

---

## 핵심 패턴

### 발표 하나가 관측치 하나 — 시각은 발표 시각
```python
Observation(kind="weather_forecast", at=base, value=24시간_최고_강수확률, meta={"hours": [...]})
```
**이유**: 예보할 때(미래)를 시각으로 두면 마지막 수신 시각이 사흘 뒤가 되어, 받기가 끊겨도 수집 감시가
사흘 넘게 모른다. 시간별 값은 meta 에 두고, 맥락에는 카드의 `describe` 가 풀어 쓴 한 줄로 보인다.

### 좌표가 아니라 5km 격자로 묻는다
**이유**: 집의 좌표는 밖으로 안 나간다. 동네 날씨에는 격자로 충분하다.

### 비·눈 신호의 `at` 은 그날 0시
**이유**: 저녁(내일 아침)과 다음 날 아침(오늘 낮)이 같은 날을 본다. 게이트가 같은 `at` 을 한 번만
판단하니 하루에 한 번이다.

### 키는 세 꼴로 로그에서 지운다
`.env` 의 꼴(인코딩), 풀린 꼴, httpx 가 다시 싼 꼴 — `core/logs.url_forms`. 요청 로그에 키가 주소째 찍힌다.

---

## 금지사항

### ❌ 공공데이터포털 키를 받은 꼴 그대로 보내지 않는다
```python
# ❌ 금지 — params={"serviceKey": key}            # 포털이 준 "인코딩" 꼴(%2B…)
# ✅ 대신 — params={"serviceKey": service_key(key)}  # 풀어서 — httpx 가 다시 싼다
```
**사고 이력**: 2026-11-09 키를 붙여 볼 때 기상청·특일·경기 버스가 모두 "등록되지 않은 서비스키"(403),
서울 버스가 401 이었다. `.env` 에 들어간 것은 포털의 "인코딩" 키였고, 그대로 보내니 두 번 싸였다. 풀어서
보내자 넷 다 200.

---

## GC 패턴

```gc
pattern: "from src\.sectors\.(?!weather)"
message: "섹터는 다른 섹터를 모른다 — 집 위치는 조립(app/main.py)이 넘겨준다"
```

```gc
pattern: "serviceKey.\s*:(?!\s*service_key\()"
message: "공공데이터포털 키는 service_key() 로 풀어서 보낸다"
```

---

## 하네스

```
tests:
  - tests/unit/test_weather.py
```

```bash
python scripts/harness.py src/sectors/weather/
```

---

## 모듈 지도

| 파일 | 책임 |
|------|------|
| `kma.py` | 기상청 단기예보 — 격자 바꾸기(`grid`), 받을 수 있는 가장 최근 발표(`base_time` — 발표 10분 뒤부터), 키 풀기(`service_key`), 시간별 읽기(`parse_forecast`, `Hour`), 받기(`fetch_forecast` — 정상이 아니면 무엇이 왔는지와 함께 예외) |
| `holidays.py` | 특일 정보 — 한 해 공휴일(`fetch_holidays`, 하나도 없으면 예외), 목록·한 줄 둘 다 읽기(`parse_holidays`) |
| `metrics.py` | 예보 카드(발표 시각, 끊김 7시간, 서버가 받음), 맥락 한 줄(`describe_forecast` — 오늘 남은 시간·내일의 기온과 비·눈 오는 때, 날짜는 요일까지 코드가) |
| `triggers.py` | 비·눈 신호(줄기 `weather`) — 저녁 20~22시엔 내일 아침 06~10시, 아침 06~09시엔 오늘 낮 07~21시. 7시간 넘은 예보는 안 본다 |
| `pull.py` | 서버가 가져오는 둘 — 예보(한 시간마다, 새 발표가 있을 때만, 집을 모르면 `NotReady`), 공휴일(올해 — 11월부터 내년도, 받은 해도 30일 지나면 다시, 받으면 `core/days` 표를 바로 바꾼다) |
