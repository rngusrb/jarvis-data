# jarvis-data

## 프로젝트 개요

초개인화 AI 에이전트("자비스")를 위한 **데이터 수집/파싱 파이프라인** 프로젝트.

Apple 생태계(Apple Watch, iPhone, Mac)에서 개인 데이터를 자동 수집하고,
패턴 분석 + 프로액티브 추천이 가능하도록 가공/저장하는 것이 목표.

## 관련 프로젝트

- **local-llm-agent** (https://github.com/rngusrb/local-llm-agent)
  - LangGraph StateGraph 에이전트 + FastAPI 서버
  - Open WebUI → FastAPI(:8001) → vLLM(:8000) 구조
  - 이 jarvis-data에서 수집한 데이터를 나중에 에이전트의 RAG 도구로 연결할 예정

## 인프라 (데스크탑 서버 — Ubuntu, RTX 3090)

- vLLM: 포트 8000 (DeepSeek R1 Distill Llama 70B AWQ)
- FastAPI Agent: 포트 8001
- Open WebUI: 포트 3000
- SearXNG: 포트 8888
- Qdrant (벡터 DB): 포트 6333 ← 이 프로젝트에서 데이터 저장 대상
- Langfuse: 포트 3001
- Tailscale IP: 100.98.90.38

## 프로젝트 구조

```
jarvis-data/
├── src/
│   ├── pipelines/    ← 데이터 수집 파이프라인 (cron/webhook으로 자동화)
│   ├── parsers/      ← 원본 데이터 파서 (XML, JSON, SQLite)
│   └── storage/      ← 저장소 연동 (Qdrant, PostgreSQL/SQLite)
├── data/
│   ├── raw/          ← 원본 데이터 (gitignore됨)
│   └── processed/    ← 가공된 데이터 (gitignore됨)
└── tests/
```

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

1. Apple Health XML 파서 구현 (`src/parsers/health.py`)
2. 파싱된 데이터를 DataFrame으로 변환
3. 기본 통계/패턴 분석
4. Qdrant에 저장하는 storage 레이어
