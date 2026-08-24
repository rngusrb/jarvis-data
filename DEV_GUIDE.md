# DEV_GUIDE — 아키텍처 지도

## 두 층, 한 방향

```
┌─ 플랫폼 (섹터가 뭐든 안 바뀐다) ──────────────────────┐
│  core      도메인 모델 · 지표 카드 · 접는 계산 · 설정   │
│  storage   SQLite 저장/조회                            │
│  brain     게이트 → 맥락 → 추론 → 발화 결정            │
│  channels  텔레그램 (→ 추후 iOS 푸시)                  │
│  runtime   자비스 루프 · 수신구                        │
│  triggers  변화 감지 (순수 함수)                       │
└───────────────────────────────────────────────────────┘
                      ↑ 섹터만 위를 쓴다
┌─ 섹터 (계속 늘어난다) ────────────────────────────────┐
│  sectors/health/   수면 · 걸음수 · 휴식기 심박          │
│  (예정) commute · shopping · schedule                  │
└───────────────────────────────────────────────────────┘
                      ↑
              app/main.py — 섹터를 아는 유일한 파일
```

**판별 규칙**: "쇼핑 섹터를 추가할 때 이 파일을 열어야 하나?"
열어야 하면 섹터, 아니면 플랫폼.

---

## 루프가 세 개 돈다

| 루프 | 주기 | 하는 일 |
|------|------|---------|
| 수집 (push) | 기상 시 | 아이폰 단축어 → `/ingest/*` |
| 수집 (백필) | 수동 | `export.xml` → `src/sectors/health/backfill.py` |
| 자비스 | 30분 + 수집 직후 | 관측 조회 → 트리거 → 게이트 → LLM → 발송 |

애플 건강은 **push 만 가능하다** — HealthKit 은 기기 밖 조회 API가 없다.
iMessage·캘린더·GitHub 처럼 pull 이 되는 소스는 `src/sectors/<x>/` 에 수집기를 둔다.

---

## 데이터 흐름

```
아이폰 단축어 / export.xml
        ↓
   runtime/ingest        지표 카드를 조회해 접는 법을 알아낸다
        ↓
   core/folding          겹침 병합 · 기기 중복 제거 · 하루치 집계
        ↓
   storage/sqlite        (source, kind, at) 멱등 쓰기
        ↓
   runtime/loop          주기 + 수집 직후
        ↓
   triggers              평소 대비 편차 감지 → Insight
        ↓
   brain/gate            severity · 쿨다운 (싼 필터)
        ↓
   brain/agent           맥락 조립 → vLLM → 발화 결정
        ↓
   channels              텔레그램
```

---

## X를 바꾸려면 어디 봐라

| 하고 싶은 것 | 볼 곳 |
|-------------|-------|
| **지표 추가** (예: 혈중산소) | `src/sectors/health/metrics.py` 에 카드 한 장 |
| **섹터 추가** (예: 쇼핑) | `src/sectors/shopping/` 폴더 + `app/main.py` 등록 한 줄 |
| 접는 법 바꾸기 | `src/core/folding.py` — 백필·수신구가 같이 쓴다 |
| 감지 기준 바꾸기 | `src/triggers/` — 백테스트 수치를 근거로 |
| 말투·판단 기준 | `src/brain/prompts.py` |
| 자비스를 똑똑하게 | `src/brain/providers.py` 에 맥락 제공자 추가 |
| 알림 채널 교체 | `src/channels/` + `app/main.py` 한 줄 |
| LLM 서버 교체 | `src/brain/client.py` (`Reasoner` 프로토콜) |
| 수신구 추가 | `src/runtime/ingest.py` — 지표 이름은 박지 않는다 |
| 저장소 교체 | `src/storage/` (`ObservationSource`·`ObservationCatalog`) |

---

## 교체 가능하게 뚫어둔 이음매

| 프로토콜 | 지금 | 나중 |
|---------|------|------|
| `Channel` | 텔레그램 | iOS 푸시 |
| `Reasoner` | vLLM 직접 | 도구가 필요하면 LangGraph(:8001) |
| `ContextProvider` | 관측 추이 · 발화 기록 · 수집 현황 | 캘린더 · 프로필 |
| `ObservationSource` | SQLite | PostgreSQL |
| `Trigger` | 수면 급감 · 수집 중단 | 이동 · 쇼핑 |
| `Fold` | 구간 · 합계 · 평균 | 최대/최소 등 |

---

## 관련 프로젝트

**local-llm-agent** — LangGraph 에이전트 + FastAPI(:8001).
자비스는 이걸 **거치지 않고** vLLM(:8000)을 직접 부른다. 판단에 필요한 재료
(관측치·발화 기억·캘린더)가 전부 이 레포에 있어 맥락 조립도 여기서 해야 하기 때문이다.
도구 실행(웹검색 등)이 정말 필요해지면 `Reasoner` 구현만 :8001 로 바꾼다.

---

## 데이터 소스 로드맵

| 소스 | 방식 | 상태 |
|------|------|------|
| Apple Health (수면·걸음·심박) | push (단축어) | ✅ |
| iMessage | pull (`~/Library/Messages/chat.db`) | 미착수 |
| 캘린더 | pull (CalDAV/.ics) | 미착수 |
| 위치 (이동관리) | push (단축어 위치 자동화) | 미착수 |
| 구매 이력 (쇼핑) | pull (이메일 영수증) | 미착수 |
