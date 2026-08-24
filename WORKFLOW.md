# WORKFLOW — 작업 프로토콜

## 태스크 관리 규칙

- TASKS.md 에는 **현재 태스크 최대 3개**만 (완료 본문 금지 — harness Doc Lint 실패)
- 완료 → `docs/sprints/` 아카이브 → BACKLOG.md 에서 다음 올림

### 태스크 연장 vs 백로그

| 기준 | 처리 |
|------|------|
| 현재 테스트 통과에 직접 영향, 2~3줄 이내 | 현재 태스크 연장 |
| 별도 파일/모듈 영향, 설계 고민 필요 | BACKLOG.md |

---

## 멀티에이전트 구성

복잡한 작업(신규 섹터, 구조 변경):

```
1. Maker    → task-start → 구현 → harness {folder}/
2. Reviewer → 변경 파일 + 테스트 결과 + _GUIDE.md 기준 read-only 검토
            → PASS / REVISE / BLOCKED 판정 선언 (형식 필수)
3. [REVISE]  → Maker 재작업 (1회 한도) → Reviewer 재검토
   [BLOCKED] → 즉시 사용자 보고, 자동 진행 금지
   [PASS]    → harness all → sprint-close
```

**Reviewer PASS 없이 sprint-close 진입 금지.**

> 현재 이 프로젝트는 사용자가 Reviewer 역할을 겸한다. 별도 세션 리뷰는
> 구조 변경이나 수치가 걸린 판단에서만 쓴다.

---

## 이슈 심각도

| 심각도 | 의미 | 처리 |
|--------|------|------|
| 🔴 Critical | 결과 무효화 (값이 틀림, 전제 붕괴) | BLOCKED → 즉시 보고 |
| 🟠 High | 수치·동작 크게 왜곡 | REVISE → 재작업 필수 |
| 🟡 Medium | 구조적 문제, 영향 제한적 | 진행 가능, TASKS.md 에 known issue |
| 🟢 Low | 개선 여지 | 기록 선택 |

동일 에러 2회 연속 → 설계 재검토.
**Medium 은 묻히면 안 된다.** 반드시 문서에 남긴다.

---

## Silent Failure 체크

```
□ except 로 삼키고 넘어가는 곳이 있는가
□ 빈 값 반환 시 호출자가 알아챌 수 있는가
□ 기본값 fallback 에 로그가 있는가
□ "정상처럼 보이는 실패"가 가능한 경로가 있는가
```

> 이 프로젝트에서 실제로 난 사고 대부분이 마지막 항목이었다 —
> `written: 0`, `"None"` 발송, 조용히 멈춘 수집.

---

## 데이터 값이 걸린 변경일 때

수집·집계·트리거 임계값을 바꿀 땐 **백테스트 수치를 근거로 남긴다.**

```
창 8일   → 10회 발동 (12.5일에 한 번), 판단불가 3일
창 21일  → 13회 발동 ( 9.6일에 한 번), 판단불가 2일
```

"좋아진 것 같다"는 근거가 아니다.

---

## Skills

| 스킬 | 트리거 |
|------|--------|
| `task-start` | 새 태스크 시작 |
| `harness-run` | 코드 수정 후 검증 |
| `maker-review-loop` | 복잡한 태스크 시작 |
| `sprint-close` | 스프린트 완료 |
