# SETUP — 처음부터 돌리기까지

자비스는 **네 조각**이 따로 돈다. 하나씩 세우고 각 단계 끝에서 확인한다.

```
아이폰 단축어  ──HTTP──┐
맥 수집기      ──HTTP──┼──→  자비스 서버 (8100)  ──→  vLLM (8000)
                       │              │
                       │              └──→  텔레그램
                    (Tailscale)
```

> ⚠️ **데이터는 공유하지 않는다.** `data/*.db` 에는 수면·위치·브라우저 기록이
> 들어 있고 gitignore 되어 있다. 같이 개발하더라도 **각자 자기 인스턴스를
> 돌린다.** 코드는 공유하고 데이터는 안 한다.

---

## 0. 무엇이 필요한가

| | 왜 |
|---|---|
| 리눅스 서버 (GPU) | vLLM + 자비스 상주. 노트북은 자면 데이터가 샌다 |
| 아이폰 + 애플워치 | 수면·심박·위치의 출처 |
| 맥 (선택) | 브라우저 기록 수집기가 여기서 돈다 |
| Tailscale | 폰이 집 밖에서도 서버에 닿아야 한다 |

**GPU가 없으면** vLLM 대신 다른 OpenAI 호환 서버를 `JARVIS_BRAIN_URL` 에
꽂으면 된다. 자비스는 `/v1/chat/completions` 만 쓴다.

---

## 1. 서버 세우기

```bash
git clone git@github.com:rngusrb/jarvis-data.git ~/jarvis-data
cd ~/jarvis-data
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env    # 없으면 아래 표 보고 직접 만든다
```

### .env

| 키 | 설명 |
|----|------|
| `JARVIS_BRAIN_URL` | vLLM 주소. 기본 `http://localhost:8000` |
| `JARVIS_BRAIN_MODEL` | 비우면 `/v1/models` 로 자동 탐색 |
| `JARVIS_INGEST_TOKEN` | **직접 만든다.** `openssl rand -hex 32` |
| `JARVIS_TELEGRAM_BOT_TOKEN` | 비우면 콘솔로 출력 |
| `JARVIS_TELEGRAM_CHAT_ID` | 위와 같음 |
| `JARVIS_LOOP_INTERVAL_SEC` | 기본 1800 |

**토큰은 각자 만든다.** 남의 토큰을 받아 쓰면 그 사람 서버에 데이터를 쏜다.

### 확인

```bash
.venv/bin/python scripts/harness.py all      # 이게 이 레포의 "완료" 기준
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8100
curl -s localhost:8100/health | python3 -m json.tool
```

### 상주시키기

```bash
sudo tee /etc/systemd/system/jarvis.service <<'EOF'
[Unit]
Description=jarvis
After=network-online.target

[Service]
User=%i
WorkingDirectory=/home/YOURNAME/jarvis-data
ExecStart=/home/YOURNAME/jarvis-data/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8100
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now jarvis
```

**vLLM도 자동 기동을 켠다.** 도커로 돌린다면:

```bash
docker update --restart unless-stopped vllm
```

> 사고 이력: 컨테이너를 새 이미지로 다시 만들면 이 정책이 `no` 로 돌아간다.
> 이미지를 올린 뒤에는 다시 걸어야 한다.

---

## 2. 아이폰 단축어 — 여기가 제일 손이 많이 간다

폰 설정은 코드로 관리할 수 없다. 이 절이 유일한 기록이다.

### 2-1. 감싸개 단축어 하나를 만든다

자동화마다 단축어를 따로 만들면 개수가 폭발한다. **HTTP로 쏘는 단축어는
하나만** 두고, 자동화가 "누가 불렀는지"를 라벨로 넘긴다.

`이동감지` 라는 단축어:

```
1. 입력으로 "텍스트"를 받기                    ← 라벨이 여기로 들어온다
2. 현재 위치 가져오기
3. 텍스트:
   {"source":"shortcuts","kind":"location","samples":[
     {"at":"⟨현재 날짜 ISO 8601⟩","value":0,
      "meta":{"lat":⟨위치 위도⟩,"lon":⟨위치 경도⟩,
              "trigger":"⟨단축어 입력⟩"}}]}
4. URL 콘텐츠 가져오기
   URL      http://100.x.x.x:8100/ingest/samples
   방식     POST
   헤더     Authorization: Bearer <INGEST_TOKEN>
            Content-Type: application/json
   본문     파일 → (3번 텍스트)
```

그리고 자동화마다 **한 줄짜리 감싸개**를 만든다:

```
[leave_home]  텍스트 "leave_home" → 이동감지 실행
[wallet]      텍스트 "wallet"     → 이동감지 실행
```

> `trigger` 라벨이 없으면 위치가 한 덩어리로 섞여서 "집을 나선 지점"과
> "카드를 찍은 지점"을 구별할 수 없다. 이게 타는 역을 찾아낸 열쇠였다.

### 2-2. 기상 시 자동화 — 수면 + 휴식기 심박

`단축어 → 자동화 → 개인 자동화 → 수면 (기상할 때)`

**수면** (`/ingest/spans`) — 구간 원본을 그대로 보낸다:

```
1. 건강 샘플 찾기
     유형    수면 분석
     기간    최근 1일
2. 반복 → 텍스트로 조립
   {"spans":[{"start":"⟨시작⟩","end":"⟨종료⟩","stage":"⟨값⟩"}, ...]}
3. POST /ingest/spans
```

> **왜 합산해서 보내지 않나**: 조각 수가 측정 품질 신호다. 폰이 "4.7시간"만
> 보내면 그 정보가 사라지고 측정 실패한 밤을 걸러낼 수 없다. 서버가 집계한다.

**휴식기 심박** (`/ingest/samples`):

```
1. 건강 샘플 찾기 → 유형 "휴식기 심박수", 기간 최근 1일
2. {"kind":"resting_heart_rate","samples":[{"at":...,"value":...}]}
3. POST /ingest/samples
```

> **원본 심박을 쓰지 않는다.** 하루 표본이 361개(최대 1,157개)라 단축어가
> 반복을 끝내지 못하고 멈춘다. 워치가 이미 계산해두는 휴식기 심박은 하루 1개다.
> 걸음수도 같은 이유로 접었다 — 운동 중엔 초 단위로 기록된다.

### 2-3. 위치 자동화

| 자동화 | 트리거 | 감싸개 |
|--------|--------|--------|
| 떠날 때 | 집·직장 등을 벗어날 때 | `leave_home` |
| 지갑 | 지갑/Apple Pay 사용 시 | `wallet` |

둘 다 **"실행 전에 묻기"를 꺼야** 한다. 안 끄면 알림만 쌓이고 데이터는 안 온다.

### 2-4. 확인

자동화를 손으로 한 번 실행하고 서버에서:

```bash
journalctl -u jarvis -f | grep 수집
```

`표본 수집 — location 1개 → 1일치` 가 보이면 된다.

---

## 3. 맥 수집기 — 브라우저 기록

```bash
python collectors/chrome_history.py --hours 24 --dry-run   # 뭐가 갈지 먼저 본다
JARVIS_INGEST_TOKEN=... python collectors/chrome_history.py --hours 24
```

한 시간마다 자동으로:

```bash
cp collectors/launchd/ai.jarvis.chrome.plist ~/Library/LaunchAgents/
# 안의 경로·토큰을 고친 뒤
launchctl load ~/Library/LaunchAgents/ai.jarvis.chrome.plist
tail -f /tmp/jarvis-chrome.log
```

> 프로필을 자동 탐색한다. 크롬은 계정을 추가할 때마다 `Profile N` 을 만들고
> `Default` 는 몇 달 전에 멈춰 있는 일이 흔하다.

---

## 4. 텔레그램 — 마지막이지만 제일 중요하다

**이걸 안 붙이면 자비스가 판단을 해도 아무에게도 닿지 않는다.**

> 사고 이력: 2026-08-29 수집이 멈췄고 자비스는 9월 12일까지 매일 경고했다.
> 텔레그램이 없어서 24번의 경고가 전부 콘솔에만 남았고, 한 달을 몰랐다.

1. 텔레그램에서 `@BotFather` → `/newbot` → 토큰 받기
2. **만든 봇에게 아무 말이나 먼저 보낸다.** 텔레그램은 사용자가 먼저 말을
   걸지 않은 상대에게 봇이 메시지를 못 보낸다. 이걸 빼면 다음 단계가 빈 값이다.
3. `chat_id` 알아내기:
   ```bash
   curl -s "https://api.telegram.org/bot<토큰>/getUpdates" | python3 -m json.tool | grep -A3 '"chat"'
   ```
4. `.env` 에 두 값을 넣고 `sudo systemctl restart jarvis`
5. 이 경고가 **사라지면** 성공: `텔레그램 설정이 없다 — 콘솔로 출력한다`

---

## 5. 회고 돌려보기

흔적이 쌓이면 손으로 한 번 돌린다. 아직 자동으로 안 돈다.

```bash
.venv/bin/python -m app.reflect --days 7
.venv/bin/python -m app.reflect --show     # 지금 믿음만 본다
```

---

## 막혔을 때

| 증상 | 볼 곳 |
|------|-------|
| 폰에서 아무것도 안 옴 | 단축어 "실행 전에 묻기" 꺼졌나 / Tailscale 켜졌나 |
| `404 Not Found` | 서비스가 옛 코드를 물고 있다 → `systemctl restart jarvis` |
| `401` | `Authorization: Bearer <토큰>` 헤더 확인 |
| 자비스가 아무 말도 안 함 | 텔레그램 설정 / `journalctl -u jarvis | grep 주기` |
| 판단이 계속 실패 | vLLM이 로딩 중일 수 있다. 27B는 10분 걸린다 |
| 값이 이상함 | 해당 섹터의 `_GUIDE.md` 금지사항부터 읽는다 |

---

## 다음에 읽을 것

| | |
|---|---|
| [CLAUDE.md](../CLAUDE.md) | 핵심 원칙. 제일 먼저 |
| [DEV_GUIDE.md](../DEV_GUIDE.md) | 아키텍처 지도, "X를 바꾸려면 어디 봐라" |
| [WORKFLOW.md](../WORKFLOW.md) | 태스크 lifecycle |
| 각 폴더 `_GUIDE.md` | **그 폴더의 금지사항과 사고 이력.** 고치기 전에 읽는다 |
