# collectors/ — 폴더 가이드

## 역할

**맥에서 도는** 수집기들. 서버(`src/`, 데스크탑)와 따로 배포되고 따로 돈다.

| 수집기 | 하는 일 | 도는 방식 |
|--------|---------|-----------|
| `chrome_history` | 크롬 방문 기록을 읽어 `web_visit` 흔적으로 보낸다 | launchd, 한 시간마다 |
| `mac_apps` | 앞에 뜬 앱을 30초마다 보고 사용 구간을 보낼 상자에 적는다 | launchd, 계속 |
| `forward` | 폰(iCloud 파일)과 보낼 상자의 줄을 서버로 보낸다 | launchd, 5분마다 |

**이 폴더가 소유하지 않는 것**: 받은 것을 어떻게 접고 저장할지(→ `src/runtime/ingest.py`),
어떤 종류가 있는지(→ 각 섹터의 카드). 수집기는 **배달만** 한다.

---

## 핵심 패턴

### 맥 시스템 파이썬(3.9) + 표준 라이브러리만
```python
from collectors.common import post   # ✅
from src.core.models import Trace     # ❌ 서버 코드는 3.11 문법과 pydantic 을 쓴다
```
**이유**: launchd 는 로그인 셸을 거치지 않아 anaconda 가 안 보인다. `/usr/bin/python3`
로 돈다. 저장소 루트에서 `-m collectors.<이름>` 으로 실행한다.

### 어디까지 보냈는지 기억하고, 성공한 뒤에만 옮긴다
```
크롬     visits.id (몇 번째 줄까지)      상태 폴더/chrome_history.json
배달부   파일마다 바이트 위치 + 앞부분 지문   상태 폴더/forward.json
앱       지금 열린 구간                 상태 폴더/mac_apps.json
```
**이유**: 서버가 꺼져 있거나 연결이 끊긴 동안의 데이터를 잃지 않는다. 상태 폴더는
`~/Library/Application Support/jarvis/` 이다 — 저장소를 지워도 커서가 산다.
서버는 같은 것을 여러 번 받아도 안전하므로(멱등), 애매하면 다시 보낸다.

### 단축어는 파일에 적고, 보내는 건 맥이 한다
```
아이폰 단축어 → iCloud Drive/단축어/jarvis/inbox.jsonl → forward → 서버
```
**이유**: 단축어가 직접 쏘면 그 순간 폰이 서버에 닿아야 하고, 실패한 데이터는 사라진다.
`forward.py` 머리말에 사고와 줄 모양이 있다.

---

## 금지사항

### ❌ 크롬 기록을 시각으로 이어 읽지 않는다
```python
# ❌ 금지 — "마지막으로 보낸 시각 이후" 를 읽는다
# ✅ 대신 — visits.id 커서. 늦게 들어온 줄도 새 번호를 받는다
```
**이유**: 아이폰에서 본 페이지는 동기화를 거쳐 맥 DB에 늦게 들어오는데 방문 시각은
원래 시각이다. 시각 커서는 그걸 영영 건너뛴다.
**사고 이력**: 2026-10-30 실측. 2주간 다른 기기 방문 1,953건 중 6시간 넘게 늦은 게 19건,
최대 12.8시간. 그 전 수집기는 "최근 3시간"을 읽어 이런 방문을 놓쳤다.

### ❌ 접는 칸을 첫 방문 기준으로 재지 않는다
```python
# ❌ 금지 — 같은 페이지를 처음 본 때부터 30분
# ✅ 대신 — 시계에 고정된 칸(:00, :30). 새 방문이 닿은 칸은 그 칸 전체로 다시 접는다
```
**이유**: 첫 방문 기준이면 어디서부터 읽느냐에 따라 접히는 모양이 달라진다.
**사고 이력**: 2026-10 이전 수집기. 겹쳐 읽을 때 이미 접혀 보낸 방문이 새 줄로 다시
들어갔고, 방문 수가 엉뚱한 줄(바로 앞 줄)에 붙었다.

---

## 하네스

```
tests:
  - tests/unit/test_chrome_history.py
  - tests/unit/test_mac_apps.py
  - tests/unit/test_forward.py
  - tests/integration/test_ingest_traces.py
```

```bash
python scripts/harness.py collectors/
```

---

## 모듈 지도

| 파일 | 책임 |
|------|------|
| `common.py` | 상태 폴더, 토큰, 서버로 보내기. 실패를 `Retry`(나중에 다시)와 `Rejected`(내용이 틀림)로 나눈다 |
| `chrome_history.py` | 크롬 기록 → `web_visit`. id 커서, 시계 칸으로 접기, 안 닫힌 칸은 미룸 |
| `mac_apps.py` | 앞 앱 표본 → 사용 구간 → 보낼 상자. 자리 비움·잠듦·정시에서 끊는다 |
| `forward.py` | iCloud 받은 편지함과 보낼 상자의 줄 → 서버. 거절된 줄은 따로 두고 `--replay` 로 되살린다 |
| `install.sh` | launchd 에 셋을 올리고 내린다 |
| `launchd/` | plist 틀. 경로는 `install.sh` 가 채운다 |
