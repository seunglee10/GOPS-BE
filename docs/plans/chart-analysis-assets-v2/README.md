# Chart Analysis Assets v2 — 질 중심 작도 고도화 계획

Status: **검토 대기 — 이 문서는 구현 승인이 아니다**
작성 기준일: 2026-07-11
대상: `alfaka.analytics`, 독립 `chart-asset-builder`, chart-analysis asset 계약/API,
`gops-frontend` 차트·해설·개발 패널

## 1. 한 문장 결정

작도 수를 채우는 시스템을 폐기하고, **현재 차트에 영향을 주는 근거가 품질 gate를
통과했을 때만 그리는 시스템**으로 바꾼다. 유효 후보가 없으면 빈 레이어가 정상
성공이다.

## 2. 왜 전면 고도화가 필요한가

2026-07-11 로컬 NVDA 자산과 현재 차트 API를 대조해 다음을 확인했다.

- 저장된 NVDA 1D 추세선은 `2025-09-17 168.41 → 2025-09-25 173.12`, 접점 2개뿐인데
  `ray`로 남아 있었다. 현재 거리·최근 접점·독립적인 세 번째 접점 gate가 없다.
- v1 커널은 유효 추세가 없으면 최근 고저로 Range를 반드시 만들고, 컴파일러는
  H-Line 슬롯을 최대 5개까지 보충한다. 이는 품질보다 출력량을 우선한다.
- 저장 자산의 timed anchor는 `00:00:00Z`, 현재 `/api/charts/candles`의 1D 봉은
  뉴욕 시장일 기준 `04:00:00Z` 또는 `05:00:00Z`였다. NVDA 저장 자산의 timed anchor
  5개 중 현재 candle timestamp와 exact match한 것은 0개였다.
- 저장 자산의 `displayFrom`은 `2024-12-05`였지만 현재 120봉 응답의 첫 봉은
  `2026-01-16`이었다. 자산 생성 입력과 실제 렌더 입력이 달라졌는데 이를 식별할
  `inputDigest`나 candle identity 검사가 없다.
- Layer I는 LLM이 전체 pivot/level/event에서 임의의 `tool + anchorIds` 조합을 만들고,
  compiler는 존재·개수·예산만 확인한다. “유효 ID를 사용한 무의미한 작도”를 막지 못한다.
- 해설은 실제 채택 drawing과 구조적으로 연결되지 않아, compiler에서 탈락한 작도를
  설명하거나 사용자가 무엇을 봐야 하는지 빠뜨릴 수 있다.

구체적 근거와 외부 조사 결과는 [00-audit-and-research.md](00-audit-and-research.md)에
기록한다.

## 3. 제품 원칙

1. **No draw is a valid answer.** 레이어별 최소 개수와 종목당 총 작도 개수를 두지 않는다.
2. **Hard gate before score.** 데이터·앵커·현재 관련성·침범 검사를 통과한 후보끼리만
   점수 경쟁을 한다.
3. **현재 관련성 우선.** 오래된 선도 현재 가격과 가까우며 최근에 재검증됐을 때만
   남긴다. 단순히 화면 밖에서 시작했다는 이유로 탈락시키지는 않는다.
4. **봉 identity 일치.** 이벤트·피벗 anchor는 실제 서빙 candle과 같은 canonical
   timestamp를 사용한다. 자동 작도도 수동 작도와 같은 봉 중심에 놓인다.
5. **LLM은 큐레이터다.** LLM은 좌표·도구·가격·사실 문장을 만들지 않고, 결정론적으로
   완성된 candidate/fact/condition ID와 강조 순서만 선택한다.
6. **그림과 해설은 한 계약이다.** 모든 표시 drawing에는 `무엇/왜/확인/무효`가 연결되고,
   표시되지 않은 후보는 해설하지 않는다.
7. **작고 빠르게.** bounded 후보 생성, 심볼당 LLM 1회, 동일 digest no-op, 선택 근거만
   저장한다.

## 4. 목표와 비목표

### 목표

- NVDA에서 확인된 오래된 2점 ray, 먼 H-Line 채우기, 반복 Flag를 제거한다.
- 1D/1W/1M에서 구조·추세·이벤트가 동일한 품질 언어와 멀티 타임프레임 문맥을 쓴다.
- 추세선·채널·Range·레벨·이벤트를 전문 투자자가 검증 가능한 근거와 함께 제시한다.
- H-Line canvas label에서 가격을 제거하고 가격축을 단일 가격 표면으로 쓴다.
- 자동 timed anchor를 실제 봉에 정확히 정렬하고, 불가능하면 보간하지 않고 보류/폐기한다.
- 해설을 `현재 구조 → 차트에서 볼 것 → 확인/무효 → 상위 주기 → 데이터 한계`로 확장한다.
- 개발 패널 문구와 입력 안내를 요청대로 정리한다.
- 실 `OPENAI_API_KEY`로 고정 real-data canary를 실행해 Layer I 품질을 검증한다.

### 이번 구현에서 하지 않을 것

- 질문창 또는 `AgentOrchestrator` workflow/roles/providers 연결
- 레거시 `/api/llm/*`, Python `chart_command/*`, 프런트 chart agent/compiler 제거·재사용
- 자동 갱신 CronJob이나 candle-closed 토픽 구독
- 주문·매수/매도 방향·포지션·`riskRewardBox` 자동 제안
- `commentary.enrichment` 실시간 보강
- 터치 툴팁
- OpenAI Batch 전환(후속 scale gate로만 기록)
- 새 ML/통계 라이브러리 도입. 필요한 robust 계산은 bounded pure Python으로 구현한다.

## 5. 문서 읽기 순서

| 순서 | 문서 | 결정 내용 |
| --- | --- | --- |
| 0 | [00-audit-and-research.md](00-audit-and-research.md) | 로컬 재현, 코드 원인, 조사 근거와 적용 한계 |
| 1 | [01-quality-contract.md](01-quality-contract.md) | v2 자산/후보/품질/마이그레이션 계약 |
| 2 | [02-canonical-data-and-anchors.md](02-canonical-data-and-anchors.md) | 캔들 identity, coverage preflight, timestamp/grid 정렬 |
| 3 | [03-structure-and-events.md](03-structure-and-events.md) | pivot, 레벨/zone, H-Line, Flag, 지표 추천 |
| 4 | [04-trend-and-range-engine.md](04-trend-and-range-engine.md) | robust 추세선·채널·Range와 현재 관련성 gate |
| 4A | [APPENDIX-A-kernel-algorithms.md](APPENDIX-A-kernel-algorithms.md) | pivot/level/event/trend/range 초기 config, pseudocode, tie-break, reason code |
| 5 | [05-agent-and-commentary.md](05-agent-and-commentary.md) | Layer I 후보 선택기, 1회 MTF 호출, 풍부한 해설 |
| 6 | [06-pipeline-storage-efficiency.md](06-pipeline-storage-efficiency.md) | 빌드 흐름, no-op, 저장·토큰·연산 절감, 운영 상태 |
| 7 | [07-frontend-and-ops-ux.md](07-frontend-and-ops-ux.md) | anchor 적용, 3버튼, 해설·개발 패널, 요청 문구 |
| 8 | [08-validation-and-rollout.md](08-validation-and-rollout.md) | real-data eval, 품질 수치, 검증 명령, 단계적 rollout |

Goal 모드 입력문은 [GOAL_PROMPT.md](GOAL_PROMPT.md)에 둔다.

## 6. 구현 묶음과 커밋 경계

각 묶음은 테스트·문서·운영 계약까지 완결한 뒤 한 커밋으로 만들고 push하지 않는다.

| 묶음 | 범위 | 핵심 종료 조건 | 권장 커밋 메시지 |
| --- | --- | --- | --- |
| A | 01+02 | v1/v2 계약, canonical candle identity, coverage fail-closed, digest, anchor parity | `feat(chart-assets): add v2 quality and canonical candle contract` |
| B | 03+04+Appendix A | 구조/이벤트/추세 전면 교체, 정상 빈 레이어 허용, NVDA deterministic 회귀 통과 | `feat(chart-assets): rebuild structure trend and event engines` |
| C | 05 | allowlisted ID형 Layer I, 심볼당 1회 MTF, drawing-grounded commentary, 안전한 degraded 경로 | `feat(chart-assets): curate visual candidates and grounded commentary` |
| D | 06 | symbol 중심 compact/no-op pipeline, 부분 성공, 저장·서빙 및 운영 문서 | `feat(chart-assets): optimize builder storage and serving pipeline` |
| E | 07+08 | H-Line/anchor/UI/개발 패널 연결, real-data/실키 canary, 품질·성능 rollout gate | `feat(chart-assets): align asset geometry commentary and ops ux` |

묶음 중 threshold를 바꾸면 `KERNEL_VERSION`, 품질 설정 버전, golden/eval 결과를 같은
커밋에서 갱신한다.

## 7. 보존 계약과 하드 제약

- `AGENTS.md`의 보존 API와 기존 차트·주문·에이전트 동작을 유지한다.
- 기존 chart-analysis asset route는 shape-compatible하게 확장하고 삭제/rename하지 않는다.
- 신규 코드는 레거시 금지 경로를 import·호출·수정하지 않는다.
- `AgentOrchestrator` workflow/roles/providers를 수정하지 않는다. 빌더는 계속 독립 워커다.
- Redis에는 자산 본문·후보를 저장하지 않는다. 현재 잡 상태 키 1개와 pubsub만 사용한다.
- DDL이 불필요한 payload-only v2를 기본안으로 한다. DDL을 건드릴 사유가 생기면 두 사본을
  byte-identical하게 수정하고 contract checker를 통과한다.
- 로컬 Python은 루트 `.venv`의 Python 3.12만 사용한다.
- 운영/로컬 runtime에 가짜 시장 캔들을 만들지 않는다. 평가 fixture도 실제 canonical
  candle snapshot만 사용한다.
- DESIGN.md 승인 타이포 롤만 사용하고 새 프런트 테스트 프레임워크를 도입하지 않는다.
- `.env`와 API key는 읽을 수 있지만 출력·로그·artifact·커밋하지 않는다.
- 현재 dirty 프런트 변경은 사용자 작업이다. 구현자는 덮어쓰지 않고 별도 pure module과
  최소 충돌 patch로 통합한다(07 문서 참조).

## 8. 완료 정의

다음을 모두 만족해야 v2 구현이 `canary-ready`다. 실제 100 symbols/S&P500 승격은 08의
human holdout gate 뒤 별도 운영 결정이며 구현 완료가 production rollout을 뜻하지 않는다.

- 08 문서의 hard invariant·운영 hard budget 100%, quality gate 통과와 benchmark SLO 측정
- NVDA 고정 회귀: 기존 2점 ray 탈락, timed anchor exact match, H-Line 가격 label 0건
- 좋은 데이터에서 의미 있는 후보만 보이고, 기준 미달 구간은 정상적으로 빈 레이어
- Layer I가 좌표·도구를 출력하지 않고 심볼당 최대 1회 호출
- 실제 표시 drawing 전부가 commentary `focusItems`와 연결
- 동일 build intent/outcome에서는 LLM·ClickHouse write가 없고, post-call 동일 content는 write 없음
- 저장 payload와 prompt/token 목표 충족
- 전체 Python/프런트/contract/build 검증 통과
- 실키 canary 및 degraded 경로 모두 통과
- 관련 canonical/platform 문서와 v2 `IMPLEMENTATION_NOTES.md` 갱신
- 묶음 A~E 로컬 커밋 완료, push 없음
