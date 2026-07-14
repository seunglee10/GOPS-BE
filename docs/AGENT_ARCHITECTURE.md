# GOPS Agent Architecture

이 문서는 GOPS 에이전트 자체를 설명하는 기준 문서다. 백엔드 연동은
`AGENT_BACKEND_INTEGRATION.md`, 프런트 연동은
`AGENT_FRONTEND_INTEGRATION.md`, AWS 빌드와 배포는 `AGENT_AWS_BUILD.md`를
따른다.

## 목적

GOPS 에이전트는 사용자 질의를 받아 시장 데이터, 뉴스, 온톨로지 관계, SEC 재무
근거를 모은 뒤 분석 리포트와 UI 제안을 만든다. 에이전트는 분석과 제안만
담당하며 주문 실행, 계좌 제어, 브로커 호출은 담당하지 않는다.

제품 방향은 다음 문장으로 정리한다.

```text
종목을 찾는 사람에게 기준을, 시장을 읽는 사람에게 방향을.
```

## Ownership Boundary

`systems/agent-orchestration`가 소유하는 범위:

- user query understanding
- `AgentOrchestrator` workflow
- market, news, relationship, financial snapshots
- role agent findings
- final answer synthesis
- analysis report serialization
- async worker, report store, delivery gateway contract
- market-event detection and notification-decision publishing
- provider adapters for market/news/ontology/financial/macro evidence

소유하지 않는 범위:

- frontend chat or panel rendering
- FastAPI auth/session policy
- AWS resource provisioning
- market-data and SEC fundamentals ingestion, backfill, and storage workers
- order, account, KIS, broker-control flows

에이전트는 절대 실주문을 실행하지 않는다. 주문 관련 의사결정이 필요하면
분석 근거와 사용자 확인을 위한 UI 제안까지만 만든다.

가격 예약 주문도 이 경계를 유지한다. 에이전트는 원문 답변에서 가격을 다시
추출하지 않고, 인증된 분석 요청의 구조화된 차트 봉으로
`tradeConditionProposals[]`를 결정론적으로 만든다. 이 제안에는 안정적인
`proposalId`, 종목, 매수/매도, 발동 방향·가격, 지정가, 수량 누락 여부와 30분
만료 시각만 담긴다. 후속 사용자 문장을 해석하고 조건을 저장하거나 주문을
실행하는 책임은 API/order runtime에 있으며 AgentOrchestrator에는 없다.

## Runtime Flow

`AnalysisReport` may include a versioned `coachReport`. The public request contains only
the lightweight `coachRequest`; the authenticated analysis worker, not the client, builds
one immutable `CoachInputSnapshot` from a completed user/date-scoped post-market S3 input
archive plus cutoff-safe ClickHouse context. It does not query orders, KIS, paper trading,
or Redis at panel-open time. The archive must provide its own `sourceAsOf`/`generatedAt`;
missing fills, portfolio pairs, and decision evidence remain explicit rather than being
invented. Deterministic coach analytics owns similarity, MFE/MAE/return, portfolio impact,
habit aggregation, improvement priority, and condition evaluation. Narrative synthesis may
explain these values but may not recompute them.

Each fill keeps `decisionAt` separate from `filledAt`. Decision evidence and similarity
inputs use `decisionAt`; entry anchoring and outcome performance use `filledAt`. A missing
decision record means `확인 기록 없음`; the coach does not add purchase-time confirmation UI
or infer a user action from chart evidence.

`coach-report.v2` exposes four UI pages. Page 2 computes `entry`, `exit`, and
`portfolio` reports independently for `30d`, `90d`, and `1y`; page 4 is the single
action center that combines the former execution, guardrail, and alert-management pages.
For entry and exit, page 2 also carries a deterministic long-term profile: confirmed versus
unconfirmed process/outcome cohorts, repeated missed-check or concentration patterns, and
representative historical trades. Profit never upgrades an unconfirmed process. Missing
decision records remain explicit rather than becoming an inferred investor trait.
The page-2 portfolio profile also exposes snapshot-bound sector exposure, holding market/
sector sensitivity, and at most three market-diversification candidates. Candidate market
facts (correlation and relative strength) must be supplied by stored point-in-time market
evidence; without them the report shows a data-connection state rather than a generic
allocation or an LLM-generated recommendation. Suggested ranges are review ranges only and
never create orders or rebalance the account.
Daily-trade alert candidates preserve their deterministic condition value, threshold,
operator, reason, recommended action, and support flag so page 4 can render the full
condition without recomputing it in the browser. Page 1 renders only a compact preview.
Exit-habit findings are likewise deterministic and conservative: pre-sale giveback uses
only completed `T-60..T-1` daily highs, while a post-sale MFE observation is eligible only
after the complete `T+1..T+20` window exists. Post-sale data is labeled hindsight outcome
evaluation and never proves that the original exit decision was wrong or that a plan was
followed when no plan-confirmation record exists.

```mermaid
flowchart LR
  Client["Frontend or API client"] --> Backend["Backend API"]
  Backend --> Queue["Kafka agents.analysis-requests.v1"]
  Queue --> Worker["agent-analysis-worker"]
  Worker --> Orchestrator["AgentOrchestrator"]
  Orchestrator --> Understanding["query understanding"]
  Understanding --> Snapshots["market/news/relationship/financial snapshots"]
  Snapshots --> Roles["role agents"]
  Roles --> Synthesis["final answer synthesis"]
  Synthesis --> Store["Redis report store"]
  Synthesis --> Results["Kafka agents.analysis-results.v1"]
  Results --> Delivery["agent-delivery-gateway"]
  Delivery --> Updates["Redis pubsub"]
  Store --> Backend
  Updates --> Backend
```

`AGENT_ASYNC_ANALYSIS_ENABLED=false`이면 compatibility mode로 백엔드가
`agent-orchestrator` HTTP endpoint를 직접 호출할 수 있다. 기본 배포 방향은
Kafka queue, worker, Redis report store를 쓰는 async path다.

## Core Components

| Component | Role |
| --- | --- |
| `AgentOrchestrator` | 질의 이해, snapshot 수집, role agent 실행, synthesis를 묶는 workflow 진입점. |
| query understanding | 종목, 관계 종목, 테마, content task, UI task, route mode를 추출한다. |
| snapshots | market/news/relationship/financial/macro evidence를 bounded provider call로 수집한다. |
| role agents | 시장, 뉴스, 온톨로지, 재무, 리스크 등 역할별 finding을 만든다. |
| synthesis | evidence와 role finding을 기반으로 최종 답변과 리포트를 만든다. |
| report store | `analysisId`별 리포트, latest report, idempotency mapping, cancel marker를 저장한다. |
| delivery gateway | result topic을 Redis update channel로 fanout한다. |
| trade condition proposal builder | 구조화된 최근 차트 봉에서 만료되는 매수·매도 가격 제안을 만든다. 주문은 실행하지 않는다. |

UI-only layout 명령은 LLM 없이 `intent_understanding/ui_parser.py`의 lexicon/rule
경로에서 먼저 판정한다. 새 action은 `intent_understanding/schema.py`와
`orchestration/ui_intent.py`의 action 집합을 함께 갱신하고, proposer는 실제 상태
변경이 불가능한 경우 `autoApply=false`와 구체적인 이유를 반환한다. Lexicon은
backend process 안에서 cache되므로 alias 변경을 배포한 뒤 process restart가 필요하다.

`shared/gops_agents/orchestrator.py`는 compatibility import shim이고, 실제
workflow 구현은 `shared/gops_agents/orchestration/` 아래에 있다.

사용자 중단은 cooperative cancellation이다. API가 `canceled` terminal report와
cancel marker를 저장하면 worker/orchestrator는 단계 경계에서 이를 확인하고,
이미 시작된 외부 provider/LLM 호출은 기존 timeout 안에서 반환되더라도 결과가
`canceled` report를 덮어쓰지 못하게 한다.

## Deterministic Safety Guardrail

Agent runtime은 외부 moderation 서비스 없이 deterministic sanitizer를 적용한다.
사용자 입력, provider evidence, synthesis payload, 최종 답변, serialized report의
문자열은 이메일, 전화번호, 한국 주민등록번호 후보, 계좌/API key/token/secret
패턴, URL query/fragment, 제한된 욕설 denylist를 마스킹한다. 마스킹이 발생하면
`finalResponse.risk_warnings` 또는 `agentTrace.inputGuardrail.warnings`에
`pii_redacted`, `profanity_removed`, `sensitive_url_redacted` 같은 원인 코드만
남기고 원문 값은 저장하거나 반환하지 않는다.

## Provider Boundary

Provider는 외부 데이터 fetch boundary다. 최종 답변 생성기나 UI command
engine이 아니다.

고정 방향:

```text
external code -> GOPS provider adapter -> ProviderRequest -> list[EvidenceItem]
```

외부 팀 코드나 ontofront branch의 데이터 로직을 가져올 때도 GOPS
architecture를 교체하지 않는다. 뉴스/온톨로지 검색, structured facts,
relationship rows, citations만 provider adapter로 감싼다.

```python
@dataclass
class ProviderRequest:
    symbol: str
    intent: str
    symbols: tuple[str, ...] = ()
```

| Field | Meaning |
| --- | --- |
| `symbol` | primary ticker, 예: `NVDA`. |
| `intent` | 원문 사용자 의도. retrieval hint로만 사용한다. |
| `symbols` | multi-symbol query나 관계 분석에 쓰는 확장 ticker 목록. |

```python
@dataclass
class EvidenceItem:
    provider: str
    status: str
    title: str
    summary: str
    observedAt: str
    url: str | None = None
    raw: dict[str, Any] = {}
```

| Field | Requirement |
| --- | --- |
| `provider` | `market`, `news`, `ontology`, `financial`, `macro` 같은 evidence source. |
| `status` | `available` 또는 `no-data`. |
| `title` | 짧은 evidence 제목. |
| `summary` | 최종 답변 문장이 아니라 사실 요약. |
| `observedAt` | source timestamp가 있으면 그것을 쓰고, 없으면 UTC ISO timestamp. |
| `url` | 뉴스나 source URL이 있으면 보존한다. |
| `raw` | ranking, citations, freshness, relation type, graph metadata 등 디버그 가능한 원천 메타데이터. |

Provider 실패나 빈 결과는 예외로 전체 분석을 멈추지 않는다. 반드시
`status="no-data"` evidence로 degrade한다.

AI 코치의 `StoreCoachPointInTimeContextProvider`는 일반 role provider fan-out과
별도로 Snapshot Builder가 요청당 한 번만 호출한다. 외부 SEC/Yahoo/Alpaca API를
hot path에서 호출하지 않고 Redis/ClickHouse에 저장된 quote, company metadata,
news, SEC fundamentals, Yahoo earnings rows와 현재 GraphDB evidence를 하나의 cutoff
계약으로 정규화한다. Quote는 event/insert 순서를 함께 사용해 결정론적으로 고르고,
체결 이후·cutoff 이하이면서 기본 96시간 freshness 안에 있을 때만 쓴다. Redis row는
별도 received/inserted 시각을 증명할 때만 적격이다. 현재 writer처럼 event time만
보존하면 ClickHouse trade tick과 완료 1분봉 순서로 degrade한다. SEC schema가 filing 시각이
아닌 날짜만 보존하므로 진입 당일 filing은 역사적 판단 근거에서 제외한다.

Daily candle similarity features are revision-aware: the selected canonical candle revision
must have `inserted_at <= decisionAt`. Display/outcome candles may use revisions available by
the immutable request cutoff, never later revisions. A provider without this vintage contract
may supply display rows but is ineligible to supply similarity features.

GraphDB는 현재 graph만 제공하므로 `temporalScope="current-only"`와
`historicalSimilarityEligible=false`로 기록하고 역사 유사도 입력에 넣지 않는다.
Yahoo `ReplacingMergeTree` row도 과거 revision 재현을 보장하지 않으므로
`historicalRevisionAvailable=false`로 표시한다. cutoff에 맞는 저장 row가 없으면
현재 값을 과거 사실로 대체하지 않고 snapshot `missingData`를 남긴다.

## Provider Types

| Provider | Runtime dependency | Behavior |
| --- | --- | --- |
| Market | Redis, ClickHouse | chart/quote/candle/order-flow 기반 snapshot을 만든다. |
| News | Redis, ClickHouse, optional Alpaca fallback | Redis 30일 cached news intelligence를 먼저 쓰고 coverage가 부족하면 ClickHouse rows로 보강한다. |
| Ontology | GraphDB, optional Redis path cache | `GraphDBOntologyProvider`가 기업 관계, peer, theme, path evidence를 만든다. |
| Financial | Redis, ClickHouse | SEC companyfacts/frames에서 미리 계산한 fundamentals snapshot을 evidence로 바꾼다. 사용자 요청 hot path에서는 SEC API를 호출하지 않는다. |
| Macro | none in v1 | v1에서는 intentional empty adapter다. |

News provider는 `provider="news"` evidence만 반환한다. Ontology provider는
`provider="ontology"` evidence를 반환하고 가능한 경우
`raw["relationType"]`, ticker, confidence, source URL, theme, sector, graph node
metadata를 보존한다.
Financial provider는 `provider="financial"` evidence만 반환한다. `missing_source`,
`stale`, `frame_coverage_gap`, `equity_includes_nci` 같은 상태는
`EvidenceItem.status`가 아니라 `raw["quality"]` 또는 snapshot `warnings`에
담는다. `EvidenceItem.status`는 `available` 또는 `no-data`만 사용한다.

GraphDB가 없거나 timeout이면 ontology snapshot은 no-data evidence가 되고,
market/news 근거만으로 분석은 계속된다. Kafka나 ClickHouse가 있다는 사실만으로
GraphDB ontology query가 성공하는 것은 아니다.

## Query And Route Modes

Hot path query understanding은 bounded fan-out으로 실행된다.

- Korean entity/theme resolver
- deterministic content-task rules
- deterministic UI-task rules
- optional classifier pod or OpenAI classifier

Interactive chart/news 질문은 shortcut router가 최종 판단하지 않는다. 프런트가
보낸 `references`와 `uiContext`를 먼저 `OperationIR` 후보로 모으고, 날짜·캔들·가격·
레이어·뉴스 anchor 계산은 deterministic resolver가 처리한다. 현재 v1은
`systems/agent-orchestration/shared/gops_agents/operations`에서 analysis/chart
operation 후보와 `contextWindow` spec을 만들고, `agentTrace.operationIR`에 남긴다.
LLM은 confidence가 낮거나 required slot이 비어 있는 복합 요청의 structured planner
fallback으로만 사용한다. 이 fallback은 `AGENT_OPERATION_PLANNER_PROVIDER=openai`일
때만 Responses API JSON schema로 호출하고, 실패하거나 예산을 얻지 못하면
deterministic `OperationIR`을 그대로 쓴다. 차트 변경은 영구 `ChartCommand[]`와
임시 visual overlay를 분리한다. 활성 차트가 있는 `차트 분석해줘`와 chart reference가
있는 `이 봉 분석해줘`는 classifier/planner를 건너뛰고 각각 `chart_overview`,
`reference_anchor_analysis`로 라우팅한다. 이때 chart role은 범용
`market_snapshot`이 아니라 PostgreSQL Geometry 자산과 canonical candle을 조합한
`chart_analysis_snapshot`을 사용한다. Geometry를 읽지 못하면 request의 bounded
화면 candle로 degrade하며, chart 설명 자체에는 LLM을 사용하지 않는다.

`chart_analysis_snapshot`의 `chartExplanation v1`은 패턴·확인 상태, 지지·저항,
trade scenario와 무효화 조건, SMA60/120 교차, 선택 봉 feature, focus drawing ID,
coverage를 typed fact로 보존한다. 최종 문장과 숫자는 deterministic Korean narrator가
렌더링한다. 뉴스는 anchor window에서 `availableAt` cutoff를 통과한 항목만 원인 후보로
정렬하고 이후 항목은 후속 뉴스로 분리하며, 인과가 아니라 시간상 연관으로 표현한다.
`analysisMode=deep`도 실제 available evidence domain이 둘 이상일 때만 LLM budget 1회를
열며, 그보다 적으면 deterministic synthesis를 사용한다.

일반 분석의 최종 사용자 답변은 `final answer synthesis`를 우선한다.
`AGENT_MAX_REALTIME_LLM_CALLS`의 운영 기본값은 2이며, runtime은
`synthesis`/`financial-synthesis` 호출 1회를 예약해 intent classifier, operation
planner, role answer 호출이 최종 종합 답변 예산을 소진하지 못하게 한다. synthesis가
API key, provider 설정, 예산, timeout, 응답 형식 문제로 OpenAI를 쓰지 못하면
deterministic fallback으로 degrade하되 `timing.synthesisProvider`,
`timing.synthesisSkippedReason`, `timing.synthesisFallbackReason`와
`agentTrace.synthesis`에 이유를 남긴다. `finalAnswer.summary`는 근거 조회 상태가
아니라 종합 판단/결론 문장이어야 한다.
분석형 최종 답변은 사용자에게 추가 비교나 확인을 넘기는 checklist/to-do 섹션을
만들지 않는다. OpenAI synthesis가 사용자에게 직접 작업을 지시하는 문장을
반환해도 서버 후처리에서 제거한다. 사용자-facing 본문에는 실제 사용한
snapshot/지표 종류를 `분석한 지표` 섹션으로만 보여준다.

Route mode:

```text
analysis
ui_layout
hybrid
clarify
```

UI layout proposal은 panel type singleton 가정만으로 배치하지 않는다. `layoutWeight`
와 최근 요청 대상을 함께 사용해 priority-aware reflow를 만들며, chart panel은
기본 priority가 높다. 명시적인 chart add/compare 요청은 기존 chart를 유지하고
symbol-bearing chart panel을 추가하거나 낮은 priority chart panel을 재사용할 수
있다.
Chart shortcut entity resolve는 단일 `symbol` 호환 필드와 다중 요청용 `symbols`
목록을 함께 낼 수 있으며, 프런트는 다중 요청을 분석 fallback이 아니라 chart add
layout proposal로 처리한다.
Layout resolve는 UI 관련 표현이지만 대상/동작이 확정되지 않은 요청을 분석
fallback으로 보내지 않고 `ui_clarify`로 빠르게 되돌려 사용자에게 구체화를
요청한다.

Company and theme resolution은 catalog 기반이다.
`config/entity-aliases.json`이 운영 alias catalog다.
`config/entity-aliases.seed.json`과 seed constants는 bootstrap fallback이며
운영 source of truth가 아니다. agent runtime은 시작 시 catalog/index cache를
warm하고, 회사 지원 여부는 alias 존재 여부가 아니라 market-data symbol
registry/universe 기준으로 검증한다.
`KoreanEntityResolver`를 직접 실행하는 runtime은 agent pods뿐 아니라
`gops-backend` entity resolve shortcut route도 포함하므로, 모두 운영 alias
catalog를 image/runtime filesystem에 포함해야 한다.

## Runtime Units

| Runtime | Required | Role |
| --- | --- | --- |
| `agent-orchestrator` | yes | HTTP compatibility endpoint and direct report lookup. |
| `agent-analysis-worker` | yes | hot analysis request를 소비하고 report를 저장한다. |
| `chart-asset-builder` | no | PostgreSQL queue의 symbol/interval item을 처리한다. ClickHouse 완료 봉을 우선 읽고 누락 range만 Alpaca로 보충한 뒤 지지·저항과 삼각형·깃발형·페넌트·직사각형·쐐기·채널 이탈을 결정론적으로 계산해 PostgreSQL에 저장한다. S3, Redis, Kafka, LLM을 사용하지 않으며 interactive orchestrator와 독립이다. |
| `agent-delivery-gateway` | yes for async/SSE | result event를 Redis report update로 mirror한다. |
| `agent-intent-classifier` | no | ambiguous query를 위한 optional cheap classifier. |
| `deep-analysis-worker` | no | opt-in deep analysis request를 처리한다. |
| `event-detector` | no | market Kafka topics를 agent market events로 바꾼다. |
| `notification-publisher` | no | notification decision, market event, risk event를 Redis/WebSocket consumer에 fanout한다. market event는 `level`/`severity`가 watch 이상일 때 기본 toast 대상으로 승격된다. |
| `graph-expansion-refresh` | no | GraphDB hint를 Redis/ClickHouse cache로 materialize한다. |
| `sec-companyfacts-backfill` | no | SEC companyfacts bulk ZIP을 S3에 저장하고 ClickHouse/Redis fundamentals projection을 만든다. |
| `sec-fundamentals-reconcile` | future | ClickHouse 최신 revision과 Redis cache를 비교해 stale cache를 재작성한다. Hot path stale check를 하지 않는다. |
| smoke/eval jobs | no | queue, store, graph, latency, retrieval, grounding checks. |

Agent runtime은 `gops-agent-orchestrator` image를 공유한다. SEC
companyfacts backfill은 S3/ClickHouse helpers를 재사용하기 위해
`gops-market-storage` image에서 실행된다.

`event-detector`의 가격 급변 판정은 trade 가격을 계속 사용하지만, 거래량
급증 판정은 `market.layer.candles.<interval>.closed.v1`의 완료 캔들만
사용한다. 같은 symbol과 interval의 이전 완료 캔들 20개 rolling 평균을
기준으로 하며, 최소 5개가 쌓이기 전에는 판정하지 않는다. 같은
symbol/interval의 `volume_spike`는 기본 30분 cooldown을 적용한다.

## Package Layout

```text
systems/agent-orchestration/
  config/                 UI lexicon, operational entity aliases, fallback aliases
  shared/gops_agents/
    contracts/            report, evidence, route, snapshot dataclasses
    query_understanding/  Korean-first entity/theme resolution
    intent_understanding/ content/UI intent decomposition
    orchestration/        workflow, routing, timing, cache, tracing
    runtime/              queues, workers, report store, delivery
    retrieval/            graph expansion, snapshots, bulkheads
    providers/            news, ontology, macro adapters and caches
    roles/                logical role agents and AgentContext
    synthesis/            final answer synthesis
    events/               market event detection and notifications
  pods/                   runtime entrypoint wrappers
  jobs/                   smoke, refresh, benchmark, eval entrypoints
  tests/                  unit and contract tests

systems/fundamentals/
  jobs/sec-companyfacts-backfill/
  shared/fundamentals/    SEC concept map, deterministic metrics, Redis keys, DDL contracts
  tests/                  fundamentals normalization and metric tests
```

Current provider implementation still imports `alfaka.*` helpers from
`systems/market-data/shared` for Kafka JSON IO, ClickHouse/Redis market
providers, news relevance, Alpaca news fallback, and ClickHouse writes. If this
dependency is removed later, create agent-owned provider interfaces first.

## Important Contracts

완료 `AnalysisReport`는 선택적으로 다음 필드를 포함한다.

```text
tradeConditionProposals[]
  proposalId, analysisId, symbol, exchange
  side, direction, triggerPrice, limitPrice, quantity
  executionEnabled, alertsEnabled, validity
  missingFields, rationale, createdAt, expiresAt
```

프런트는 이 값을 가격 조건으로 직접 저장하지 않는다. 사용자의 명시적인 후속
요청이 있을 때 API가 report owner와 proposal ID를 다시 검증해야 한다.

Kafka topics:

```text
agents.market-events.v1
agents.analysis-requests.v1
agents.deep-analysis-requests.v1
agents.analysis-results.v1
agents.query-understanding-events.v1
agents.notification-decisions.v1
agents.dlq.v1
```

`NotificationDecision`은 선택적 `eventType`을 포함한다. 프런트는 이 값을
가격 급등락, 거래량 급증 같은 사용자 알림 설정에 매핑하며, 알 수 없는 값은
전체 알림과 기업별 알림 gate만 적용한다.

Redis report keys and channels:

```text
agent:report:{analysisId}
agent:report:latest:{SYMBOL}
agent:report:latest
agent:request:idempotency:{userHash}:{keyHash}
agent:report:cancel:{analysisId}
agent:report:owner:{analysisId}
agent.reports
agent.reports:{analysisId}
gops:agent:graph-expansion:v1:{symbol}
gops:agent:graph-path:{...}
gops:fundamentals:summary:v1:{SYMBOL}
gops:fundamentals:peer:v1:{SYMBOL}:latest
gops:fundamentals:peer:v1:{SYMBOL}:{FRAME_PERIOD}
```

Fundamentals Redis cache is trusted on the agent hot path. Stale detection is
performed by SEC backfill/reconcile jobs, not by querying ClickHouse on every
Redis hit.

ClickHouse tables used by agent providers:

```text
market_data.symbols
market_data.news_articles
market_data.news_article_localizations
market_data.news_company_daily_summaries
market_data.agent_graph_expansions
market_data.sec_company_tickers
market_data.sec_filing_events
market_data.sec_raw_artifacts
market_data.sec_financial_facts
market_data.sec_derived_metrics
market_data.sec_frames
market_data.sec_collection_runs
market_data.chart_analysis_assets  # PostgreSQL cutover 전 compatibility/rollback projection
```

Financial role contract:

```text
role = financial
agent id = financial-agent
finding role = financial-analysis
evidence provider = financial
```

Snapshot bundle additions:

```text
financial_analysis -> financial_snapshot, risk_policy_snapshot
financial_comparison -> financial_snapshot, financial_peer_snapshot, risk_policy_snapshot
financial_news_analysis -> financial_snapshot, news_snapshot, risk_policy_snapshot
```

## Validation

```sh
git diff --check
.venv/bin/python -m unittest discover -s systems/agent-orchestration/tests -p 'test_*.py'
.venv/bin/python -m unittest discover -s systems/fundamentals/tests -p 'test_*.py'
```

Backend bridge changes should also run:

```sh
.venv/bin/python -m unittest discover -s systems/api-server/tests -p 'test_agent_routes.py'
```
