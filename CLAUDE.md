# jarvis-data

## 프로젝트 개요

초개인화 AI 에이전트("자비스")를 위한 **데이터 수집/파싱 파이프라인** 프로젝트.

Apple 생태계(Apple Watch, iPhone, Mac)에서 개인 데이터를 자동 수집하고,
패턴 분석 + 프로액티브 추천이 가능하도록 가공/저장하는 것이 목표.

## 관련 프로젝트

- **local-llm-agent** (https://github.com/rngusrb/local-llm-agent)
  - LangGraph StateGraph 에이전트 + FastAPI 서버
  - Open WebUI → FastAPI(:8001) → vLLM(:8000) 구조
  - 자비스는 이 레포의 에이전트를 거치지 않고 vLLM(:8000)을 직접 호출한다
  - 도구 실행(웹검색 등)이 정말 필요해지면 그때 `Reasoner` 구현만 :8001로 바꾸면 된다

## 인프라 (데스크탑 서버 — Ubuntu, RTX 3090)

- vLLM: 포트 8000 (Qwen3.8-27B-FP8, TP=2, `--reasoning-parser qwen3`)
- FastAPI Agent: 포트 8001
- Open WebUI: 포트 3000
- SearXNG: 포트 8888
- Qdrant (벡터 DB): 포트 6333 ← 이 프로젝트에서 데이터 저장 대상
- Langfuse: 포트 3001
- Tailscale IP: 100.98.90.38

## 프로젝트 구조

이 레포는 데이터 파이프라인 + 프로덕트(자비스 본체)를 함께 담는 모노레포다.

**자비스의 에이전트는 이 레포(`src/brain`)에 있다.** `local-llm-agent`(:8001)의
LangGraph 에이전트를 거치지 않고 vLLM(:8000)을 직접 부른다 — 판단에 필요한 재료
(건강 관측치, 발화 기억, 캘린더)가 전부 여기 있으므로 맥락 조립도 여기서 해야 한다.
저쪽은 순수 추론기로만 쓴다.

```
jarvis-data/
├── src/
│   ├── pipelines/    ← 데이터 수집 파이프라인 (cron/webhook으로 자동화)
│   ├── parsers/      ← 원본 데이터 파서 (XML, JSON, SQLite)
│   ├── storage/      ← 저장소 연동 (Qdrant, PostgreSQL/SQLite)
│   ├── core/         ← 도메인 모델 (Observation, Insight, Severity) — 전 레이어 공유
│   ├── triggers/     ← 변화 감지 룰 (순수 함수, I/O 금지)
│   ├── brain/        ← 자비스 에이전트 (아래 참고)
│   └── channels/     ← 발신 채널 (텔레그램 → 추후 iOS 푸시)
├── app/              ← 자비스 루프 (감지 → 판단 → 발송 조립)
├── data/
│   ├── raw/          ← 원본 데이터 (gitignore됨)
│   └── processed/    ← 가공된 데이터 (gitignore됨)
└── tests/
```

### 루프가 두 개 돈다
- **수집 루프**(배치, cron): 수집 → 파싱 → 정규화 → 저장
- **자비스 루프**(주기적, `app/loop.py`): 최근 관측 조회 → 트리거 감지 → 게이트 → LLM 판단 → 발송

### src/brain 내부

```
client.py     ← Reasoner 프로토콜 + vLLM(:8000) 구현. <think> 블록 제거 포함
memory.py     ← SpeechLog: 언제 무슨 말을 했는지 (게이트와 맥락이 공유)
gate.py       ← 싼 필터. severity 임계값 + 트리거별 쿨다운
context.py    ← ContextProvider 프로토콜 + 조립
providers.py  ← 실제 맥락 제공자 (관측 추이, 최근 발화)
prompts.py    ← 프롬프트 템플릿 + SKIP 파싱
agent.py      ← JarvisAgent: 게이트 → 맥락 조립 → 추론 → 발화 결정
```

자비스를 똑똑하게 만드는 건 더 큰 모델이 아니라 `providers.py`에 제공자를
하나씩 늘리는 일이다. 도구 실행(캘린더 잡기 등)이 필요해지면 `agent.consider()`
안에서 루프가 자란다 — `app/loop.py`는 그때도 안 바뀐다.

### 레이어 규칙
- `triggers/`는 네트워크·DB·LLM을 건드리지 않는다 (테스트 가능성 + 재현성)
- 발화 판단은 **싼 게이트(severity·쿨다운) 먼저, 비싼 LLM 나중** — 알림 스팸이 이 프로덕트의 주된 실패 방식
- 모델 응답에서 `content`가 `null`일 수 있다 — 사고 중 `max_tokens`에 걸린 경우다.
  `str(None)`이 되면 `"None"`이 그대로 발송되므로 반드시 걸러낸다
- 이상 감지는 절대 기준이 아니라 **개인 baseline 대비 편차**로 한다

## 데이터 소스 (수집 대상)

### 1순위 — 바로 시작 가능
- **Apple Health (건강 데이터)**: 아이폰 건강 앱 → XML Export → 파싱
  - 심박수, 걸음수, 수면, 운동, 혈중산소 등
  - 파일: `export.xml` (수백MB 가능)
- **iMessage**: Mac `~/Library/Messages/chat.db` SQLite 직접 읽기
- **캘린더**: CalDAV API 또는 .ics 파일

### 2순위 — 약간의 작업 필요
- 스크린타임 (아이폰 Shortcuts 자동화)
- 위치 기록 (Shortcuts + GPS 로깅)
- 카카오톡 채팅 내보내기
- 브라우저 기록 (Safari/Chrome SQLite)

### 3순위 — 추후
- GitHub 활동
- 노션/메모
- 사진 EXIF 메타데이터

## 파이프라인 설계 방향

- 데이터 파이프라인 설계에 심혈을 기울일 것 (이 프로젝트의 핵심 학습 목표)
- 수동 입력이 아니라 **능동적 자동 수집** 지향
- 흐름: 수집 → 파싱 → 정규화 → 임베딩 → 저장 (Qdrant + 정형DB)
- Apple Watch + iPhone은 항상 휴대하므로 24시간 라이프로그 장치로 활용

## 사용자 참고

- 사용자는 학습 목적으로 진행 중 — 코드 설명과 "왜 이렇게 하는지" 설명을 원함
- 캐주얼 한국어로 소통
- 사소한 수정(lint/typecheck)은 알아서 처리 OK
- CI: PR 기반 워크플로우 (lint + typecheck + test)

## 다음 할 일

1. Apple Health XML 파서 구현 (`src/parsers/health.py`) → `Observation` 리스트로 반환
2. `ObservationSource`를 만족하는 저장소 구현 (SQLite부터, 이후 Qdrant)
3. 자비스 루프 실전 배선 (`app/main.py` — 설정 로드 + 채널 선택 + 스케줄 실행)
4. 텔레그램 봇 토큰 발급 후 `.env` 세팅, 실제 발송 확인
5. 트리거 추가 (심박, 활동량) + Qdrant 임베딩 저장
