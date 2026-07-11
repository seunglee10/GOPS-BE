# Chart Analysis Assets

이 문서는 GOPS가 차트에 무엇을, 왜, 어떻게 제안하는지 사람이 검토할 수 있도록
설명하는 현재 설계 문서다. 구현 세부 기준은
[`CHART_ANALYSIS_ASSETS_CODEX.md`](CHART_ANALYSIS_ASSETS_CODEX.md)를 따른다.

## 한 문장 정의

Chart Analysis Asset은 과거 차트에 선을 많이 긋는 기능이 아니라, **현재 가격 판단에
영향을 주는 검증된 구조만 골라 정확한 봉 위에 표시하고 확인·무효화 조건까지 함께
전달하는 사전 계산 자산**이다.

```mermaid
flowchart LR
  Candle["정규화된 실제 봉"] --> Kernel["결정론적 분석 커널"]
  Kernel --> Gate{"현재도 유의미한가?"}
  Gate -- "아니오" --> Empty["정상 무작도"]
  Gate -- "예" --> Geometry["커널 소유 좌표"]
  Geometry --> Curator["선택 전용 LLM curator"]
  Curator --> Commentary["근거·확인·무효화 해설"]
  Commentary --> Asset["compact v2 asset"]
  Asset --> Chart["차트 3개 레이어"]
```

핵심 원칙은 양보다 질이다. 품질 기준을 통과한 후보가 없으면 빈 레이어가 정답이다.

## 전체 시스템 흐름

```mermaid
sequenceDiagram
  actor Operator as 운영자
  participant API as Chart Asset API
  participant Kafka as build topic
  participant Worker as chart-asset-builder
  participant CH as ClickHouse
  participant S3 as S3 canonical final
  participant Store as Active asset store
  participant Kernel as analytics kernel
  participant LLM as OpenAI curator
  participant UI as Chart frontend

  Operator->>API: symbol/interval/LLM 옵션으로 수동 빌드
  API->>Kafka: symbol 중심 job 발행
  Worker->>Store: 기존 8개 interval asset snapshot 1회 조회
  Worker->>Worker: freshness/input/version digest 검사
  Worker->>CH: 요청 interval의 정확한 canonical 1D 범위 감사
  opt 결측 구간 존재
    Worker->>S3: symbol inventory LIST 1회 후 관련 object manifest 조회
    S3-->>Worker: 요청 범위 object를 memory에 prepare (DB write 없음)
    Worker->>CH: deadline 안에 끝난 prepare만 caller thread가 commit
    Worker->>CH: 남은 구간만 Alpaca split 1D로 materialize
    Worker->>CH: canonical 1D 재감사
  end
  Worker->>CH: 검증된 daily bundle 1회 조회
  alt 동일한 빌드 의도
    Worker-->>API: unchanged
  else 계산 필요
    Worker->>Kernel: 1M → 1W → 1D → 4h → 1h → 10m → 5m → 1m feature와 S/T 후보 계산
    opt LLM 활성화
      Worker->>LLM: 좌표 없는 compact 후보 ID bundle 1회
      LLM-->>Worker: 선택 ID와 서술 참조 ID만 반환
    end
    Worker->>Worker: 좌표 materialize + 해설 조립 + schema 검증
    Worker->>Store: content가 바뀐 asset만 저장
  end
  UI->>API: GET /api/charts/analysis-assets
  API->>Store: interval별 최신 v1/v2 asset
  API-->>UI: 호환 응답
  UI->>UI: 구조/추세/인사이트 토글 및 해설 focus
```

이 빌더는 대화형 `AgentOrchestrator`와 분리된 독립 워커다. 질문창, 주문, 기존
차트 명령 경로를 호출하지 않는다. readiness/repair도 수동 build 요청의 symbol에
대해서만 그 요청 수명 안에서 실행되며 CronJob이나 candle-closed 구독은 없다.

## 데이터에서 작도까지

### 1. 실제 봉을 하나의 시간 격자로 만든다

분석은 ClickHouse의 실제 시장 데이터만 사용한다. 인트라데이는 저장된 해당 interval을
직접 읽고, 1D를 기준으로 1W와 1M을 결정론적으로 집계한다. NYSE의 마지막 실제 세션
종료 전인 봉은 제외한다. Identity는 `candleKey`이고, 인트라데이는 정확한 UTC bucket
timestamp, 1D는 뉴욕 자정, 1W·1M은 UTC bucket start다.

빌드 전 감사 범위는 요청 interval의 lookback에서 정확히 계산한다. 1D는 완료 거래일
500개, 1W는 완료 312주, 1M은 완료 72개월을 구성하는 일봉이다. 휴장일과 특별
휴장일을 포함한 공통 거래일 캘린더로 head/interior/tail 결측을 판정하고, S3로
복원되지 않은 범위만 Alpaca에 요청한다. Redis candle은 분석 입력에 섞지 않는다.

```mermaid
flowchart TD
  Raw["ClickHouse 실제 daily candles"] --> Normalize["중복 제거·정렬·session 정규화"]
  Normalize --> Daily["1D canonical candles"]
  Daily --> Weekly["완료된 1W bucket"]
  Daily --> Monthly["완료된 1M bucket"]
  Daily --> Coverage["coverage / gap / stale 검사"]
  Weekly --> Coverage
  Monthly --> Coverage
  Coverage -- "renderable" --> Features["feature 계산"]
  Coverage -- "불충분" --> Preserve["기존 asset 보존 또는 degraded empty"]
```

시간 좌표가 필요한 모든 anchor는 해당 interval의 실제 candle timestamp와 정확히
일치해야 한다. 따라서 Flag가 봉 사이를 가리키거나, 집계 시작 시각과 화면의 봉
위치가 어긋나는 좌표는 저장되지 않는다.

### 2. 구조를 후보로 만들고 강한 하드 게이트를 적용한다

| 구조 | 최소 증거 | 현재 관련성 | 화면 예산 |
| --- | --- | --- | --- |
| 지지·저항 | 독립 touch episode 3회, reaction 2회 | 현재가에서 2 ATR 이내, role 확정 | 지지 1 + 저항 1 |
| 추세선 | structural anchor 2개, raw 고·저가 독립 접점 3회, 0.75 ATR 반응 2회 | 2.25 ATR 이내, 최근 접점 20% 이내, active invalidation 없음 | 1개 |
| 채널 | confirmed 기준선 + 반대 경계 접점 2회, 평행 오차 20% 이하, containment 80% | 기반 추세의 현재 관련성 통과 | 추세 예산과 공유 |
| 박스권 | 상·하단 각 2회, 합산 5회, 최근 양 경계와 교대 반응, containment 85% | 현재가가 박스에서 0.75 ATR 이내 | 추세 예산과 공유 |
| 이벤트 | breakout/retest/gap/52주 extreme 등의 상태와 impact 검증 | interval별 age와 current impact 통과 | Flag 최대 1개 |
| 삼각형 | 상·하단 각 2회, 합산 5회, 수렴·containment·ATR residual 검증 | 형성 중 또는 예상 방향 돌파 확인 | 경계선 2개 |
| 깃발 | 4 ATR 이상 깃대, 평행 채널 각 2회, 10~50% 되돌림 | 형성 중 또는 깃대 방향 돌파 확인 | 깃대 + 채널 |

ATR은 종목 가격대와 변동성 차이를 정규화한다. 단순히 오래된 두 점을 연결하거나
화면 안에 있다는 이유만으로 선을 통과시키지 않는다.

```mermaid
flowchart TD
  Pivot["tactical + structural pivots"] --> Level["ATR 기반 가격 cluster"]
  Level --> Episode["touch를 독립 episode로 병합"]
  Episode --> Reaction["반응·실패·미결 판정"]
  Reaction --> Role["support/resistance/role flip 상태"]
  Role --> LevelGate{"3 touch + 2 reaction + ≤2 ATR"}

  Pivot --> Hypothesis["같은 종류 pivot 쌍 가설"]
  Hypothesis --> Inlier["ATR residual inlier cluster"]
  Inlier --> Reaction["raw 접점 뒤 0.75 ATR 반응"]
  Reaction --> TrendGate{"3 touch + 2 reaction + active validity"}

  Pivot --> Range["상·하단 반응과 containment"]
  Range --> RangeGate{"합산 5 touch + 85% containment + ≤0.75 ATR"}

  LevelGate --> Candidate["hardPass 후보"]
  TrendGate --> Candidate
  RangeGate --> Candidate
```

### 3. 세 레이어가 역할을 나눈다

```mermaid
flowchart LR
  Feature["검증 feature"] --> S["S · Structure"]
  Feature --> T["T · Trend/Range"]
  S --> Palette["추가 후보 palette"]
  T --> Palette
  Palette --> I["I · Insight"]

  S --> View["최종 차트"]
  T --> View
  I --> View
```

- S 레이어는 지지·저항과 현재 영향도가 높은 이벤트를 표시한다.
- T 레이어는 가장 유의미한 추세선, 채널 또는 박스 하나를 표시한다.
- I 레이어는 커널이 이미 만든 가격 구간, 되돌림, 추가 이벤트 후보 중 LLM이
  선택한 것만 표시한다.

전체 rule 작도는 interval당 최대 4개 수준이며, I 레이어까지 합쳐도 전경 예산을
넘지 않는다. H-Line 라벨에는 가격을 반복하지 않는다. 가격은 차트 가격축에서
표시된다.

## LLM이 할 수 있는 일과 할 수 없는 일

```mermaid
flowchart TB
  subgraph KernelBoundary["결정론적 신뢰 경계"]
    Candidate["candidateId"]
    Geometry["drawingTemplate geometry"]
    Fact["factId"]
    Condition["conditionId"]
    Evidence["evidenceRef"]
  end

  Candidate --> Compact["compact symbol bundle"]
  Fact --> Compact
  Condition --> Compact
  Evidence --> Compact
  Compact --> LLM["ID selector"]
  LLM --> Validate{"모든 ID가 허용 집합인가?"}
  Validate -- "아니오" --> Degrade["deterministic empty selection"]
  Validate -- "예" --> Materialize["서버가 원본 geometry 복사"]
  Geometry --> Materialize
```

LLM은 좌표, 가격, 선 종류, 자유 문장을 만들지 않는다. 후보 ID가 하나도 없으면 호출도
하지 않는다. 후보 ID, 사실 ID, 확인 조건
ID, 상위 주기 관계 ID만 선택한다. 존재하지 않는 candidate/fact/condition/evidence
참조는 validator가 거부한다. API key 부재나 OpenAI 실패는 빌드 실패가 아니라
rule layer를 보존한 degraded asset이다.

## 해설은 작도와 함께 움직인다

각 최종 drawing은 `focusItems[].drawingIds`에 반드시 한 번 이상 연결된다.

```mermaid
flowchart LR
  Drawing["최종 drawing ID"] --> Focus["focus item"]
  Evidence["feature/evidence ID"] --> Focus
  Focus --> Shows["무엇을 보여주는가"]
  Focus --> Matters["왜 중요한가"]
  Focus --> Confirm["확인 조건"]
  Focus --> Invalidate["무효화 조건"]
  Focus --> Horizon["weeks / months / years"]
```

해설은 방향 예측 신뢰도를 꾸미지 않는다. `marketDirection.score`는 `null`이고,
선택 신뢰도는 geometry 검증, 현재 관련성, 데이터 coverage만 반영한다.
`commentary.enrichment`도 현재 범위에서는 항상 `null`이다.

## 저장과 연산을 줄이는 방식

```mermaid
flowchart TD
  Start["빌드 요청"] --> Fresh{"모든 asset이 freshness window 안인가?"}
  Fresh -- "예" --> Skip["fresh skip"]
  Fresh -- "아니오" --> Load["canonical input 1회"]
  Load --> PreDigest{"input/version/model/higher digest 동일?"}
  PreDigest -- "예" --> NoKernel["kernel·LLM·INSERT 0"]
  PreDigest -- "아니오" --> Kernel["kernel 계산"]
  Kernel --> IntentDigest{"선택 가능한 의미가 동일?"}
  IntentDigest -- "예" --> NoLLM["LLM·INSERT 0"]
  IntentDigest -- "아니오" --> Curate["symbol당 LLM 최대 1회"]
  Curate --> ContentDigest{"표시 content 동일?"}
  ContentDigest -- "예" --> NoInsert["INSERT 0"]
  ContentDigest -- "아니오" --> Save["active latest-asset store write"]
```

- 기존 asset은 심볼당 1회 snapshot query로 8개 interval을 함께 읽는다.
- raw candle과 전체 후보는 저장하지 않고, 선택된 evidence와 regime만 compact하게
  투영한다.
- Canonical candle과 repair materialization은 ClickHouse에 남는다. 최신 asset JSON은
  기본 ClickHouse에서 parity-guarded dual-write를 거쳐 PostgreSQL 한 행으로 이전할 수 있다.
- asset hard cap은 20 KiB, 운영 목표 p95는 12 KiB다.
- Redis에는 asset 본문을 저장하지 않는다. 24시간 build job 상태 문자열과 pub/sub
  채널만 사용한다.
- build log는 기존 pub/sub 채널로만 송출하고 Redis status 또는 ClickHouse에 저장하지
  않는다. 열린 개발 패널의 브라우저 메모리가 최근 200줄만 보유한다. interval별 S/T/I
  생성 엔티티 수, 정상 무작도 탈락 사유와 warning/failure 원인을 보여준다. 최종 생성
  엔티티 수는 로그 유실과 무관하게 status의 작은 정수 필드로도 전달한다.

## 사용자 화면

프런트는 asset을 원본 차트 데이터와 별개로 읽고 다음을 제공한다.

```mermaid
flowchart TD
  API["analysis-assets response"] --> Normalize["v1/v2 discriminator 정규화"]
  Normalize --> Snap["interval candleKey로 실제 봉 timestamp에 snap"]
  Snap --> Stale{"완료 candleKey가 asset보다 새 개인가?"}
  Stale -- "예" --> Hide["자동 적용하지 않음"]
  Stale -- "아니오" --> Apply["drawing sourceProposalId로 적용"]
  Apply --> ToggleS["구조"]
  Apply --> ToggleT["추세"]
  Apply --> ToggleI["인사이트"]
  Apply --> Panel["해설·주요 관찰 focus"]
  Panel --> Highlight["연결 drawing 강조"]
```

운영 패널은 저장·현재 차트 적용·제외 수와 이유를 구분한다. 적용 수는 자산 payload가
아니라 active chart document에 실제 존재하는 drawing ID 교집합으로 계산한다. S&P 500 전체 또는 콤마로
구분한 심볼을 수동 빌드한다. 자동 갱신,
질문창 연동, candle-closed 구독은 현재 범위가 아니다.

빌드가 끝나거나 개발 패널에서 자산을 삭제하면 cache invalidation을 구독한 열린 차트와
해설 패널이 같은 symbol을 즉시 다시 조회한다. 자산 현황은 최종 작도 수를 함께 보여
`ready · eligible · 작도 없음`과 저장 오류를 구분한다. 행별 삭제는 확인 후 해당
symbol/interval을 active asset store에서 제거하며 dual mode는 양쪽 성공을 요구한다.

## 현재 검증 상태

- 15개 실제 Nasdaq 일봉 series와 207개 as-of episode를 재사용하며, active
  `blind-investor-holdout-v1` round는 35개 chronological episode다.
- AAPL, AMZN, GOOGL, NVDA, MU는 로컬 데이터가 상대적으로 충분한 통합 표본이다.
  각 표본은 7년·1,759개 일봉을 가지며 품질 로직은 이 종목들에 특화하지 않는다.
- golden corpus에서 anchor 불일치와 H-Line 가격 라벨 위반은 0건이다.
- 최신 rules run의 deterministic kernel p95는 25.9ms, symbol bundle p95는 48.9ms,
  작도 median은 2, p95는 4다. 주기별 `must_draw` ready recall은 1M 87.5%,
  1W·1D 100%이고 전체 ready false-zero는 4.3%다.
- 자동 reviewer estimate는 precision 99.1%, clearly meaningless 0%였지만 human
  precision으로 부르지 않는다. 독립 재감사에서 임의로 만든 strict-empty 5건을
  false-empty로 판정해 제거했으며, active `must_not_draw` denominator는 0이다.
  semantic recall도 35/59(59.3%)로 목표에 못 미쳐 quality gate 전체는 미통과다.
- 현재 compact inventory는 symbol prefix LIST를 한 번만 하지만 기존 per-object
  manifest JSON은 항목별 GET한다. 시간별 `final-v2` scan은 0회지만, 장기 이력을
  단일 aggregate index 한 번으로 읽는 성능 gate는 아직 rollout blocker다.
- `.env` key를 process memory에서만 읽은 AAPL·AMZN·GOOGL·NVDA·MU 실제 OpenAI
  5-call canary는 strict output으로 모두 non-degraded 완료됐다. LLM latency는 약
  4.7~6.6초로 deterministic 350ms budget과 분리한다.
- ClickHouse → builder → ClickHouse → FastAPI serving 관통과 동일 입력 no-op을
  확인했다.
- PostgreSQL schema/dual-write/sync/parity 기능은 구현했지만 실제 schema 적용,
  backfill, 7일 관찰, read-primary cutover는 수행하지 않았고 기본은 ClickHouse다.
- 자동 rubric 결과는 회귀 신호일 뿐 전문가 평가를 대신하지 않는다. production
  rollout 전 human blind review가 필요하다.
- 로컬 Alpaca API는 사용할 수 없는 환경을 전제로 하며, 검증은 기존 ClickHouse
  데이터와 고정된 실제 공개 데이터 fixture로 수행한다. 가짜 시장 봉은 금지한다.

## 주요 코드 위치

| 책임 | 위치 |
| --- | --- |
| 정규 candle·coverage·repair | `systems/market-data/shared/alfaka/analytics/analysis_candles.py`, `analysis_repair.py` |
| pivot·level·trend·event | `systems/market-data/shared/alfaka/analytics/` |
| S/T compiler | `systems/agent-orchestration/shared/gops_agents/chart_assets/compilers.py` |
| 후보·LLM 경계 | `systems/agent-orchestration/shared/gops_agents/chart_assets/curation.py`, `llm.py` |
| 해설 | `systems/agent-orchestration/shared/gops_agents/chart_assets/commentary_v2.py` |
| symbol 중심 빌드 | `systems/agent-orchestration/shared/gops_agents/chart_assets/builder.py` |
| 저장·API | `chart_assets/storage.py`, `systems/api-server/.../routes/chart_assets.py` |
| 프런트 | `apps/gops-frontend/src/chart/analysisAssetsApi.ts`, `analysisAssetPresentation.ts` |
| 계약 | `shared/chart-contract/chart-analysis-asset-v2.schema.json` |
| 평가 | `scripts/local/eval-chart-assets-v2.py` |
