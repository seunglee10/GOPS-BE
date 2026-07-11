# Goal 모드 구현 프롬프트

아래 내용을 검토한 뒤 그대로 복사해 Goal 모드에 입력한다.

```text
/goal GOPS 저장소의 Chart Analysis Assets v2를 구현해줘. 이번 작업은 작도 수가 아니라 질적 정밀도, 현재 관련성, 봉 정합, 낮은 연산·저장 비용을 최우선으로 한다.

## 스펙 읽기 순서

1. 저장소 루트 AGENTS.md를 가장 먼저 읽고 전 과정에서 준수한다.
2. docs/plans/chart-analysis-assets-v2/README.md를 읽는다.
3. 같은 디렉터리의 00-audit-and-research.md부터 04-trend-and-range-engine.md까지 읽고,
   APPENDIX-A-kernel-algorithms.md를 완독한 뒤 05-agent-and-commentary.md부터
   08-validation-and-rollout.md까지 번호 순서대로 완독한다.
4. docs/CHART_DATA_ARCHITECTURE.md, docs/AGENT_ARCHITECTURE.md,
   docs/AGENT_BACKEND_INTEGRATION.md, docs/AGENT_FRONTEND_INTEGRATION.md,
   docs/AGENT_AWS_BUILD.md, docs/CHART_AGENT_STRATEGY.md,
   docs/STOCK_RECOMMENDATION_PANEL.md, DESIGN.md와 현재 코드를 읽는다.

이 문서 세트가 구현 스펙이자 완료 기준이다. 이전 chart-analysis-assets 계획은 복원하거나 스펙으로 사용하지 않는다. 실제 코드 경로·시그니처가 문서와 다르면 기존 컨벤션에 맞춰 최소 조정하되 계약, 품질 hard gate, API shape, key/topic 이름은 v2 문서를 우선한다.

## 구현 묶음

묶음마다 08 §1 완료 조건을 확인하고 §6의 관련 검증을 실행한 뒤 통과할 때만 아래 메시지로 커밋한다. 푸시하지 않는다.

A. 계약·정규 데이터·기하
   - v1/v2 자산 계약, canonical candle loader/aggregation, candleKey/anchor invariant, 입력 품질 gate
   - 커밋: feat(chart-assets): add v2 quality and canonical candle contract

B. 구조·이벤트·추세·범위
   - prominence pivot, level zone/touch episode, stateful event, consensus trend/channel/range, current relevance
   - 억지 슬롯 채우기와 2점 추세선 제거, ready + emptyReason인 정상 빈 레이어 정착
   - 커밋: feat(chart-assets): rebuild structure trend and event engines

C. 에이전트·해설
   - 결정론적 visual/narrative allowlist, symbol당 멀티 타임프레임 LLM 1회, ID 선택만 허용
   - allowlisted fact/condition/relation ID의 우선순위만 LLM이 선택하고 factual 한국어는 서버
     code-to-text로 조립
   - focusItems[].drawingIds 기반의 풍부한 한국어 해설과 확인/무효화 조건, 안전한 degraded 경로
   - 커밋: feat(chart-assets): curate visual candidates and grounded commentary

D. 파이프라인·저장·서빙
   - symbol 중심 빌드, digest no-op, 부분 성공, compact asset, 성능/비용 계측
   - 관련 platform README와 docs/AGENT_ARCHITECTURE.md Runtime Units 등 문서 갱신
   - 커밋: feat(chart-assets): optimize builder storage and serving pipeline

E. 프런트·운영 UX
   - asset anchor 재스냅/거부 경계, 3개 레이어 토글, 자동 지표, 해설 focus 강조, 개발 패널
   - 자동 H-Line label의 가격 중복 제거, flag 실제 봉 중앙 정합
   - `신선 자산 스킵(시간)`을 정확히 `갱신 스킵(시간)`으로 변경
   - `전체 S&P500` 행 오른쪽 정렬로 `콤마로 구분` 안내 추가. 콤마 앞뒤 공백 유무 모두 지원
   - 08의 실제 episode/holdout 최소 denominator를 만족하는 manifest,
     eval-chart-assets-v2.py, blind A/B 검토 index, invariant·성능 계측,
     rollout/rollback gate까지 검증 산출물 완성
   - 커밋: feat(chart-assets): align asset geometry commentary and ops ux

## 하드 제약

- AGENTS.md의 보호 API route와 기존 차트·주문·에이전트 동작을 보존한다. 기존 route의 의미를 바꾸지 않는다.
- systems/agent-orchestration/shared/gops_agents/chart_command/*, api-server의 /api/llm/*, apps/gops-frontend/src/agent/chartAgent.ts, apps/gops-frontend/src/agent/chartOperationCompiler.ts를 import/호출/수정하지 않는다.
- 작도 payload의 규범 계약은 shared/chart-contract/chart-command.schema.json이다.
- AgentOrchestrator workflow/roles/providers를 수정하지 않는다. 빌더는 독립 runtime이다.
- Redis에는 기존 job 상태 키 1개와 pubsub 외에 자산 본문·후보를 저장하지 않는다.
- 새 Kafka topic이나 자동 CronJob/candle-closed 구독을 추가하지 않는다.
- ClickHouse DDL이 필요하면 infra/clickhouse/initdb와 infra/k8s/base/platform/clickhouse-initdb 두 사본을 동일하게 유지한다. 가능하면 v1 테이블과 기존 row를 재사용하고 destructive migration을 하지 않는다.
- 저장소 루트 .venv(Python 3.12)만 사용한다. 가짜 시장 캔들을 runtime/smoke에 만들지 않는다.
- 새 테스트 프레임워크나 불필요한 라이브러리를 추가하지 않는다. 프런트 타이포는 DESIGN.md 승인 롤만 쓰고 로컬 font-size를 선언하지 않는다.
- 질문창/오케스트레이션 연동, 레거시 제거, 자동 갱신, riskRewardBox LLM 팔레트, 터치 툴팁은 범위 밖이다. `commentary.enrichment`는 항상 null로 유지한다.
- quantity를 맞추려고 품질 gate를 완화하지 않는다. eligible한 정상 빈 레이어는 성공 결과다.

## 작업 안전

- 시작 시 git status와 현재 diff를 확인하고 baseline patch를 별도 안전한 위치에 기록한다. 사용자가 이미 수정한 파일과 변경을 보존하고, 관련 부분을 통합할 때도 덮어쓰지 않는다.
- 현재 dirty worktree의 unrelated 변경은 stage하거나 commit하지 않는다. `git add -A`, `git add -u`를 쓰지 말고 파일·hunk 단위로 구현자 변경만 stage한다. 겹치는 파일은 staged diff에서 사용자 baseline hunk가 섞이지 않았는지 확인한다.
- 파일 검색은 rg/rg --files를 우선하고, 로컬 파일 편집은 apply_patch를 사용한다.
- 계약이나 실제 코드 차이, threshold 결정, 평가 결과, 성능 예외를 docs/plans/chart-analysis-assets-v2/IMPLEMENTATION_NOTES.md에 묶음별로 기록한다.

## 품질과 검증

- 08 문서의 자동 불변식은 100% 통과해야 한다.
- 08 §2의 실제 development/tuning corpus와 holdout 최소 denominator, 고정 NVDA 회귀를
  사용한다. split을 분리하고 미래 봉을 보지 않는다.
- 모든 timed anchor가 실제 chart candle에 대응해야 한다. interpolation으로 실패를 숨기지 않는다.
- 추세선은 독립 touch 3회 이상과 현재 관련성이 있어야 한다. 현재와 무관한 화면 밖 선은 버린다.
- H-Line 자동 label에는 가격을 쓰지 않는다. 가격은 차트 가격축이 담당한다.
- Layer I는 서버가 만든 candidate/fact/condition/relation ID만 선택하며 자유 factual 문장,
  geometry·가격·신뢰도를 생성하지 않는다.
- 한 symbol당 canonical market query 1회 이하, LLM 1회 이하로 제한한다. 입력/version/context가
  같아 증명 가능한 fast no-op은 kernel/LLM/INSERT를, kernel 뒤 intent가 같은 late no-op은
  LLM/INSERT를 생략한다. LLM 뒤 content만 같으면 INSERT/cache invalidation만 생략한다.
- 실제 .env의 OPENAI_API_KEY가 있으면 08 §6.5의 `--env-file .env` 방식으로 canary를 실행한다. shell expansion과 `set -x`를 쓰지 않는다. 키, prompt/응답 원문, 계정 식별자를 log/ClickHouse/Redis/fixture/결과에 남기지 않고, `store: false`와 strict structured output을 확인한다. `/tmp`에는 sanitize된 summary만 두며 커밋하지 않는다.
- 이번 환경처럼 key가 있으면 real smoke와 stratified pilot은 완료 필수다. key 부재나 재현된 DNS/provider 장애로 호출 자체가 불가능한 경우에만 mock structured-output 및 degraded S/T 경로로 대체하고 증거를 기록한다. 품질/안정성 gate 실패는 대체 사유가 아니며 Layer I를 비활성화한다.
- 각 묶음에서 관련 단위/계약/프런트 테스트를 실행하고, 마지막에 08 §6 전체 명령, `npm run build --prefix apps/gops-frontend`, git diff --check를 모두 실행한다.

## 완료 보고

다음이 모두 충족될 때만 Goal을 완료 처리한다.

- A~E 구현과 묶음별 커밋 5개 완료, 푸시 없음
- 08 §1 완료 조건과 §3 불변식 충족
- 08 §4 품질 목표와 §5 성능·비용·저장 기준의 측정 결과 보고
- 08 §5의 hard/regression/canary 비용·저장 gate 통과. 절대 benchmark 목표 미달은 근거와 profile 기록
- 실제 LLM smoke/pilot 완료. 단, 증명된 key 부재·외부 provider 장애일 때만 08의 degraded 대체 검증 완료
- 관련 platform README, CHART_DATA_ARCHITECTURE.md, AGENT_ARCHITECTURE.md,
  AGENT_BACKEND_INTEGRATION.md, AGENT_FRONTEND_INTEGRATION.md, AGENT_AWS_BUILD.md와
  IMPLEMENTATION_NOTES.md 갱신
- 기존 chart/order/agent 행동 회귀 없음

최종 답변에는 바뀐 핵심, 품질 전후 비교, NVDA 결과, 실 LLM 검증, 성능/저장/토큰 측정, 실행한 검증과 결과, 커밋 목록, 남은 한계를 간결하게 보고해줘.
사람 holdout 검토가 없으면 품질 수치를 automated reviewer estimate로 명시하고, 구현은
canary-ready로만 표현하며 production rollout human gate를 완료했다고 주장하지 마.
```
