# GOPS Agent Backend Integration

이 문서는 백엔드가 GOPS 에이전트 런타임을 붙일 때 지켜야 하는 계약을
정리한다. 에이전트 내부 구조는 `AGENT_ARCHITECTURE.md`를 기준으로 한다.

## Backend Role

AI coach remains on the existing analyze/report contract. `POST /api/agents/analyze`
accepts only a bounded `coachRequest` (`enabled`, optional `selectedFillId`, optional
`tradingDate`). A client-supplied `coachInputSnapshot` is stripped. The authenticated
`agent-analysis-worker` builds one trusted snapshot after consuming the Kafka envelope,
and completed polling/SSE reports may contain optional `coachReport`. The backend does
not create per-page jobs or call `AgentOrchestrator.analyze()` in the request handler.

백엔드는 에이전트 요청의 ingress와 report delivery를 담당한다.

- HTTP request validation
- user/session/idempotency policy
- async request enqueue
- compatibility HTTP call to `agent-orchestrator`
- report polling endpoint
- SSE report stream
- alert WebSocket bridge

백엔드는 request handler 안에서 `AgentOrchestrator.analyze()`를 직접 호출하지
않는다. sync compatibility가 필요하면 `agent-orchestrator` HTTP endpoint를
호출한다.

## Account Holdings Source Boundary

`GET /api/account/holdings`는 선택적 `source=active|kis`를 받으며 기본값은 `active`다.
`active`는 기존 동작을 보존해 SIM 모드에서는 시뮬레이터 원장을, 그 외에는 연결된 KIS
보유정보를 반환한다. `source=kis`는 SIM 모드와 무관하게 KIS 보유정보 경로를 사용하며
차트 해설의 실계좌 보유 표가 이 값을 읽는다. 응답 형식은 기존 account/positions 계약을
그대로 사용한다.

이 query는 조회 source만 고르며 주문 환경이나 broker 권한을 바꾸지 않는다.
`KIS_ENV=real` 비활성 정책, 주문 멱등성, 주문/outbox 경계는 그대로 유지한다. 차트 대화
기록은 서버에 저장하지 않고 API request/report 계약에도 추가하지 않는다.

## Price Condition Command Boundary

가격 조건과 예약 주문은 agent 분석 생성과 분리된 backend/order 기능이다.

```text
GET    /api/trade-conditions
POST   /api/trade-conditions
PATCH  /api/trade-conditions/{condition_id}
DELETE /api/trade-conditions/{condition_id}
POST   /api/trade-conditions/commands
```

수동 등록은 현재 서버 가격과 발동 방향, 양수 지정가, 정수 수량을 검증한다.
Agent 후속 명령은 클라이언트가 보낸 가격을 신뢰하지 않고 `analysisId`와
`proposalId`만 받아 사용자 소유 report의 `tradeConditionProposals[]`를 다시
조회한다. `걸어줘`, `등록해줘`, `예약해줘` 같은 명시적 등록 표현과 선택적 수량을
규칙으로 해석하며, 모호하거나 수량이 없거나 30분이 지난 제안은 저장하지 않는다.
같은 사용자와 proposal ID 조합은 멱등이다.

각 조건은 one-shot `alerts.price_cross` 행과 PostgreSQL transaction으로 함께
저장된다. alert 행의 `condition`에는 `{kind: price_cross, operator: above|below,
threshold}`를 저장하고 `condition_version=1`, `created_via=trade_condition`으로
출처를 명시한다. 이 필드는 alert evaluator가 같은 PostgreSQL 행을 즉시 읽을 수
있게 하며, 필수 `condition` 제약이 있는 운영 schema에서도 수동 조건 등록이
실패하지 않게 한다. 알림 끄기는 WebSocket/notification 생성만 생략하며 가격 평가와
예약 주문 이벤트는 유지한다. 일시정지는 alert 평가도 중단한다. 트리거된 조건은
다시 감시 상태로 되돌릴 수 없다.

`trade-condition-executor`는 `alerts.triggered.v1`을 별도 consumer group으로
읽고 조건을 한 번 점유한다. `sim`/`paper`는 영구 가상계좌에, `demo`는 기존
orders/outbox 계약에 같은 결정적 멱등키로 제출한다. 두 경로 모두 기존 사전 리스크
검사를 통과해야 하며, KIS 실계좌 모드는 사용하지 않는다. 같은 Kafka 이벤트가
재전달되거나 주문 접수 뒤 상태 저장이 실패해도 같은 멱등키로 복구한다.

## AI Coach Alert Proposal Boundary

AI 코치 알람 제안은 기존 `POST /api/alerts`를 재사용한다. 사용자가 4페이지에서
명시적으로 생성할 때만 optional `proposalSource`를 보낼 수 있으며 허용값은
`daily_trade`, `entry_habit`, `exit_habit`, `portfolio_risk`다. API는 이를 PostgreSQL
`alerts.proposal_source`에 저장하고 create/list 응답에서 `proposal_source`로 보존한다.
기존·수동 알람은 null이며 임의 출처를 추정하지 않는다. 이 메타데이터는 알람의
평가·주문 동작을 바꾸지 않는다.

`coach-report.v2.page4.recommendedAlerts`의 당일 거래 후보는 page 1의 결정론적
조건에서 `currentValue`, `threshold`, `operator`, `detail`, `recommendedAction`,
`alertSupported`를 그대로 복사한다. 프런트가 임계값을 다시 계산하거나 원천 데이터를
재조회하지 않으며, 미지원 조건에는 `alertRequest`를 만들지 않는다.

`coach-report.v2.page2`의 장기 투자 성향 분석은 같은 immutable snapshot의 체결·결과·
decision-check event만 사용한다. `confirmed`는 여섯 필수 확인 key가 모두 기록되고
checked인 거래만 의미하며, 나머지는 `unconfirmed`로 남긴다. 과정·결과 cohort, 반복
누락 패턴, 대표 거래는 worker가 결정론적으로 계산하고 프런트는 재계산하지 않는다.
포트폴리오 탭의 시장·섹터 분산 후보도 worker가 snapshot의 현재 보유 평가액과 저장된
market correlation/relative-strength context로만 계산한다. context가 없으면 후보·비중
범위를 반환하지 않으며, API나 LLM이 일반 섹터 추천을 대신 만들지 않는다. 후보는
검토용이며 자동 알람·주문·리밸런싱을 생성하지 않는다.

## Tick Replay Simulator Boundary

`GOPS_SIMULATOR_URL`은 고정 데이터셋 `sp500-top20-20260715-kst-v1`을 읽는 별도
서비스를 가리킨다. 백엔드는 `/api/simulator/status|mode|action|speed`만 공개하며
기존 phase, 합성 news, basket 경로는 제공하지 않는다. 가상시각은 KST
`2026-07-15 00:00`에서 시작하고 모든 사용자에게 동일하지만, 계좌·주문·가격조건은
`userId + runId`로 격리된다.

`GET /api/charts/candles`는 replay 시작 전 정상 과거 봉과 현재 가상시각까지의 replay
봉만 합친다. `/ws/charts`도 simulator candle snapshot을 묶어서 보내며 실시간 Redis
WebSocket 경로를 사용하지 않는다. SIM 심볼 검색은 manifest의 21개 티커로 제한한다.
빠른 주문은 `GET /api/simulator/quote`로 현재 replay bid/ask를 읽고 기존
`POST /api/orders`를 통해 `userId + runId` 주문 원장에 기록한다. SIM에 존재하지 않는
종목이나 아직 호가가 도착하지 않은 종목에는 주문 후보를 만들지 않는다.
기업정보·agent snapshot처럼 신뢰할 수 있는 point-in-time 조회가 없는 경로는
`409 simulation_data_unavailable`을 반환한다. 추천은 검증된 fixed replay provider가
준비된 경우에만 기존 recommendation route를 허용한다. 자동 작도 조회는
`GET /api/charts/analysis-assets` 정확한 경로만 허용하고, DELETE·coverage·build·status는
계속 차단한다.
예외적으로 `GET /api/market/news/latest`는 live Redis를 건너뛰고 ClickHouse의
`published_at <= virtualTime AND localized_at <= virtualTime`인 저장 기사만 읽는다.
`GET /api/market/news/daily`와 `GET /api/charts/events`는 SIM `virtualTime`을 cutoff로
전달해 `news_company_daily_summaries.generated_at`이 cutoff 이하인 저장 스냅샷만
읽는다. SIM daily 응답에는 미래 종가 노출을 막기 위해 최신 일봉 가격 변화를 붙이지
않는다. 이 경로들은 외부 뉴스 API를 호출하거나 live Redis 캐시를 갱신하지 않는다.

SIM의 `POST /api/orders`는 기존 `Idempotency-Key`와 리스크 검사를 유지한다.
`order_type=market`은 price를 생략할 수 있고 현재 ask/bid로 즉시 전량 체결한다.
`order_type=limit`은 price가 필수이며 조건 충족 시 실제 ask/bid로 가격 개선을 적용한다.
LIVE KIS는 기존 limit-only 계약을 유지한다. 주문 조회·event·WebSocket과
`/api/trade-conditions`는 실행별 Redis 원장으로 라우팅되며, 기존 LIVE/paper 조건은
숨기고 executor도 replay 활성 Redis 키가 있는 동안 평가하지 않는다. LIVE 전환은 해당
run namespace만 제거하며 실시간 Redis/Kafka/Alpaca 상태는 변경하지 않는다.

## Persistent Paper Trading Boundary

영구 가상투자는 전역 LIVE/SIM 상태와 무관하며 사용자 `sub`로 격리한다.
`POST /api/paper/orders`는 `Idempotency-Key`를 필수로 받고 Postgres의 가상 현금과
보유수량만 예약한다. KIS 주문 테이블, Outbox, broker adapter는 호출하지 않는다.

```text
GET  /api/paper/symbols/search
GET  /api/paper/account
POST /api/paper/account/reset
GET  /api/paper/account/balance
POST /api/paper/risk/pretrade
GET  /api/paper/orders
POST /api/paper/orders
GET  /api/paper/orders/{order_id}
GET  /api/paper/orders/{order_id}/events
POST /api/paper/orders/{order_id}/cancel
WS   /ws/paper/orders/{order_id}
WS   /ws/paper/account
```

`paper-order-matcher`는 `market.layer.quotes.v1`의 모든 market session quote를
사용한다. 매수는 ask, 매도는 bid 최우선호가로 전량 체결하며 부분체결, 수수료,
공매도는 지원하지 않는다. 모든 HTTP와 WebSocket 조회는 주문 소유자를 검사한다.
가상투자 심볼 검색은 ClickHouse의 전체 symbol registry를 직접 조회하며
`active`, `tradable`인 미국 주식/ETF만 노출한다.

AI 코치는 KIS append-only `order_coach_fill_history`에서 분석 요청시각까지 관찰된
최신 canonical cumulative fill state와 filled `paper_orders`를 같은 사용자 범위에서 읽되 namespace를 섞지
않는다. KIS fill ID는 reconciliation replay와 partial/final update에 걸쳐 안정적인
`kis:{order_id}`다. `executions`는 broker reconciliation audit log이며 개별 AI 코치
fill ledger가 아니다. `orders.coach_filled_at`/`coach_fill_payload`는 최신 상태 호환
projection이며, 지연 분석의 point-in-time 입력에는 history를 사용한다. 동일하거나
낮은 누적 수량 replay는 history를 추가하지 않고, 부분 체결 후 잔량 취소된 row의
양수 누적 체결은 canceled 최종 상태와 별개로 보존한다. Paper matcher는 체결 트랜잭션 안에서 `paper:{order_id}`에
묶인 before/after portfolio rows를 `user_portfolio_snapshot_history`에 기록한다. 이
row의 `valuationBasis="cost_basis"`는 가상 현금과 취득원가 비교용이며 현재가 평가액으로
표시하거나 해석하지 않는다. KIS와 paper 모두 정확한 `fillId` + `phase` pair가 없으면
포트폴리오 영향은 `계산되지 않음`으로 남긴다.

Snapshot Builder reads PostgreSQL fills, portfolio rows, decision events, and alerts in one
read-only repeatable-read transaction. `decisionAt` is the earliest server-owned order/check
time and bounds decision evidence; `filledAt` anchors the executed entry and subsequent
performance window. Historical decision-check rows remain attached to their matching cases.

## Order-time decision checks

`POST /api/orders`와 `POST /api/paper/orders`는 optional `decision_checks`를 받을 수
있다. 현재 order ticket과 quick-order UI가 보내는 bounded contract는 다음과 같다.

```json
{
  "version": "decision-checks.v1",
  "surface": "order-ticket",
  "items": [
    {"key": "chart.rsi", "status": "checked"},
    {"key": "chart.macd", "status": "unchecked"},
    {"key": "chart.volume", "status": "checked"},
    {"key": "news.company", "status": "unchecked"},
    {"key": "fundamentals.earnings", "status": "checked"},
    {"key": "market.context", "status": "checked"}
  ]
}
```

`surface`는 `order-ticket` 또는 `quick-order`이고 item status는 `checked` 또는
`unchecked`다. 서버는 unknown/duplicate key와 client-supplied label, category,
evidence, timestamp field를 거절하고, allowlist의 label/category와 server capture
time을 붙인 normalized JSON을 주문 row에 저장한다. 이 JSON은 broker outbox payload로
전파하지 않는다.

체결이 확정될 때만 normalized items를 `trade_decision_check_events`로 materialize한다.
KIS는 `kis:{order_id}`, paper는 `paper:{order_id}`를 fill ID로 사용하며
`(user_sub, fill_id, check_key)` unique index와 conflict-safe insert로 reconciliation
retry를 멱등 처리한다. canceled/rejected order에는 fill-scoped event가 생기지 않는다.
체크를 보내지 않은 기존 order는 추정 backfill하지 않고 AI 코치에서
`확인 기록 없음`으로 남긴다. 이 기능은 새 route나 Kafka topic을 만들지 않는다.

## Runtime Flow

```mermaid
sequenceDiagram
  participant FE as Frontend
  participant API as Backend API
  participant K as Kafka
  participant W as agent-analysis-worker
  participant R as Redis report store
  participant D as agent-delivery-gateway

  FE->>API: POST /api/agents/analyze
  API->>R: queued report + idempotency mapping
  API->>K: agents.analysis-requests.v1
  API-->>FE: 202 analysisId, status_url, stream_url
  K->>W: consume request
  W->>R: completed report
  W->>K: agents.analysis-results.v1
  K->>D: consume result
  D->>R: publish agent.reports:{analysisId}
  FE->>API: GET report or SSE stream
```

## API Routes

백엔드가 보존해야 하는 agent route:

```text
POST /api/agents/analyze
POST /api/agents/layout/resolve
GET  /api/agents/entities/resolve
GET  /api/agents/reports/{analysis_id}
POST /api/agents/reports/{analysis_id}/cancel
GET  /api/agents/reports/{analysis_id}/stream
WS   /ws/agent-alerts
```

기업 비교 패널은 별도 read path를 사용한다.

```text
POST /api/llm/company-compare
POST /api/llm/company-compare/quantitative
GET  /api/llm/company-compare/candidates?symbol=NVDA
```

POST body는 `{baseSymbol, compareSymbols[], question?}`이며 비교 종목은 1~3개다.
`company-compare.v1` 응답은 즉시 렌더링하는 `quantitative`와 `qualitative`, 후속 서술용
`narrative`, `sources`, `dataGaps`를 분리한다. `/quantitative` 경로는 이름과 달리 두 저장
근거 레이어를 모두 반환하되 LLM gateway는 호출하지 않으므로 표·차트·10-K·관계·뉴스가
서술 생성 시간을 기다리지 않는다. `narrative.status`는 `not-requested`이고 OpenAI key가
없어도 저장 근거 응답이 동작한다. 조회는 Redis/ClickHouse SEC projection, Yahoo
estimates, Redis 10-K 카드, GraphDB, ClickHouse/Redis news만 사용하며 요청 중 SEC/Yahoo
외부 API나 10-K 프로파일 생성은 실행하지 않는다. candidates route는 GraphDB
same-theme evidence를 사용한다.

두 POST route의 body는 `baseSymbol` 10자, 비교 심볼 1~3개, 선택 질문 1000자로 제한한다.
인증이 활성화되면 기존 사용자별 agent rate limit을 공유하고 초과 응답은 `429`와
`Retry-After`를 반환한다. 전체 서술 route는 데이터 revision 기반 Redis lazy cache를
사용한다. cache key에는 정렬된 심볼, 질문, 재무·실적 기준일, 10-K accession, 뉴스
revision, 안정된 GraphDB 관계가 들어가므로 어느 근거라도 바뀌면 새 서술을 만든다.
기본 TTL은 86400초다. hit 응답도 strict schema와 evidence ref를 다시 검증하며
`narrative.cache.status="hit"`을 반환한다. 같은 payload의 두 번째 요청은 OpenAI를
호출하지 않는다.

사용자 알림 표시 설정은 다음 session-auth route를 사용한다.

```text
GET   /api/notification-preferences
PATCH /api/notification-preferences
POST  /api/alerts/commands
```

설정과 기업별 override는 `user_notification_preferences`에 사용자별로 저장된다.
이 설정은 프런트 알림 바/toast 노출만 제어하며 notifications 이력, unread count,
가격 조건 평가, 주문 실행은 삭제하거나 중지하지 않는다.

`POST /api/alerts/commands`는 `Idempotency-Key`를 필수로 받고, 결정론 파서 뒤에
agent-orchestrator `/alerts/resolve`를 제한적 fallback으로 사용한다. 명확한 단일 조건은
`created_via=agent_chat`으로 즉시 생성하고, 종목·임계값·봉 간격이 빠지면 추측하지
않고 `clarificationId`를 반환한다.

news 패널과 news agent가 일자별 요약을 렌더링할 때 market-data query route
`GET /api/market/news/daily?symbol={SYMBOL}&limit=30&locale=ko-KR`를 사용할 수
있다. 응답은 `displayMode="dailySummary"`와 `dailySummaries[]`를 포함하며,
각 summary의 `sources[]`는 실제 기사 `url`, 표시용 `title`, 선택적 `name`을
가진다. 가능한 경우 각 summary에는 같은 날짜의 1D 종가와 직전 거래일 1D 종가
차이를 나타내는 `priceChange`를 포함한다. 이 public payload에는
`source="redis"` 또는 `source="clickhouse"` 같은 내부 저장소 provenance를 넣지
않는다.
읽기 경로는 Redis 30일 article/daily hot cache를 먼저 사용하고, Redis coverage
metadata가 최근 30일 요청을 보장하지 못할 때만 ClickHouse에서 보강한 뒤 Redis를
다시 warm-up한다.

기업저널 재무 시계열은
`GET /api/market/fundamentals/{symbol}/series?period=quarterly|annual`을 사용한다.
기존 손익·자산 필드와 함께 `currentAssets`, `currentLiabilities`,
`cashAndCashEquivalents`, `interestExpense`, `debtRatio`,
`currentLiabilityRatio`, `noncurrentLiabilityRatio`, `currentRatio`, `totalDebt`,
`interestCoverage`, `financialCostBurdenRatio`, `netDebt`를 반환한다. 파생 필드는
ClickHouse `sec_derived_metrics` 값을 전달하며 API 요청 시 재계산하지 않는다.

지수 패널은 market-data query route `GET /api/market/indices`를 사용한다. 이
route는 차트 candle coverage/backfill/read-through 경로를 타지 않고 Yahoo
Finance snapshot을 Redis에 fresh/stale 캐시한다. 백엔드는 fresh 캐시가 있으면
즉시 반환하고, fresh가 없을 때만 짧은 refresh lock을 잡아 Yahoo Finance를
조회한다. refresh 중이거나 Yahoo Finance가 timeout/rate-limit/failure를 반환하면
마지막 성공 snapshot을 `cacheStatus="stale"`로 반환할 수 있다.

장중 매수 추천 패널은 recommendation API를 사용한다. `PUT /api/recommendations/profile`은
추천목록 패널의 추천 설정 dialog에서 필수 투자 설정을 저장하고, `GET
/api/recommendations/stocks/latest`와 `POST /api/recommendations/stocks/refresh`는
정규장 장중 추천만 반환한다. 추천 worker는 프로필이 있는 사용자 목록을 순회하고,
09:45/12:45/15:45 ET 슬롯 run key로 멱등 실행한다. 종목 선정은 결정론적 점수화
로직이 담당하며, 알림은 기존 notifications 저장소와 `WS /ws/notifications`
브로커만 재사용한다. 추천 profile, heatmap item, recommendation item, holdings
snapshot의 `sector`는 GraphDB `gops:sector` canonical 값을 사용하고, 화면 표시용
한글 라벨은 `sectorLabelKo`로 함께 내려준다.

추천 profile의 `recommendationStyle`은 `momentum`, `balanced`, `stable` 중 하나이며
`riskLevel`과 독립적이다. personalization 활성화 시 backend는 완료 1D history,
직전 정규장 1분봉, SPY, cutoff-safe 뉴스와 추천 시점 이전
`user_portfolio_snapshot_history`를 결합한다. run에는 snapshot history ID,
`weights_version`, `personalization_input_digest`, bounded personalization metadata만
저장하고 전체 계좌 payload를 복제하지 않는다. 같은 slot의 기존 run은 digest까지
같을 때만 replay한다. item별 전문 점수와 팩터 기여도는 기존
`metrics_snapshot` JSONB에 저장해 API shape를 확장하되 기존 필드를 제거하지 않는다.
자동 주문과 시뮬레이터 경로는 이 계산에 연결하지 않는다.

명시적 `RECOMMENDATION_ALGORITHM_VERSION=legacy|professional-v1|continuous-v2|deterministic-evidence-v3`가 있으면
기존 flag보다 우선한다. `continuous-v2`는 shadow 여부와 무관하게 실제 최종 점수로
정렬하고 공개 `algorithmVersion`은 `continuous-personalization-v2`다. 추천 cutoff까지의
canonical real fill만 시간순으로 처리하며, `order_coach_fill_history`의 매수 체결을
24시간 이내 동일 종목 candidate feature와 연결한다. 매도와 match 실패도 skip reason이
있는 event로 남지만 선호 state를 바꾸지 않는다. paper/simulator activity는 포함하지
않는다.

`deterministic-evidence-v3`는 가격·수익률·성공확률을 예측하지 않는다. 같은 세션 슬롯의
전체 준비 유니버스에서 immutable evidence snapshot을 한 번 만들고, 사용자별 제외·스타일,
실제 매수 체결 기반 선호, 최신 포트폴리오 적합성만 후처리한다. 공개 `score`는
`FinalRankScore`, `confidence`는 성공확률이 아닌 `EvidenceReliability / 100`이다. 신뢰도
70 미만과 hard gate 실패 종목은 개인화로 복구할 수 없다. run은 evidence snapshot ID와
`deterministic-evidence-v3.1`의 전체 규칙 snapshot을 저장한다.

V3는 SPY 완료 일봉 252개, 직전 정규장 1분봉 380개 이상(약 390개), 세션 신선도,
Redis/ClickHouse 최신 candle 일치, 신뢰도 70 이상 후보 15개를 activation gate로 사용한다.
하나라도 실패하면 run/item을 만들거나 legacy로 fallback하지 않고
`status="data_not_ready"`, `summary.retryable=true`를 반환한다. 새 V3 item은 migration
`0014_recommendation_explanations.sql`의 `explanation_json`에
`recommendation-explanation.v1`을 저장한다. 결정론적 한국어 설명이 권위 있는 근거이고,
선택적 OpenAI Responses batched narrative는 문장만 다듬으며 실패 즉시 결정론적 문장을 쓴다.
직접 추천 v1은 migration `0015_direct_recommendation_v1.sql`의 profile history,
확장 action check와 `decision_json`을 사용한다.

AWS의 `recommendation-v3-2026-07-15` fixed replay override는 일반 장중 run과 분리된다.
`RECOMMENDATION_FIXED_REPLAY_ENABLED=true`이면 7월 14일 16:00 ET 근거로 만든 30개
candidate pool을 검증하고, `RECOMMENDATION_DECISION_V1_ENABLED=true`일 때 cutoff 이하의
프로필·포트폴리오·선호 snapshot만 읽어 사용자별 Top 15와 직접 매수 판단을 만든다.
active symbol과 세션 선택은 순위에 영향을 주지 않는다. 응답은 공통
`evidencePoolDigest`, 사용자별 `personalizationDigest`·`recommendationDigest`,
`personalizationMode=cutoff_user_context`와 action/decision/sizing/keyEvidence/cautions를 포함한다.
직접 추천 문장은 `recommendation-decision-renderer.ko.v6`가 실제 관측된 V3 값과
우선순위가 지정된 실패 조건으로 만든다. `keyEvidence`는 시장 흐름·거래 참여·가격 구조를
기본으로 하고 `availableBlocks`에 있는 뉴스·촉매, 체결 여건, 안정성·품질만 추가한다.
각 근거는 수치 문장과 함께 `metrics[]`의 표시값, 비교 기준, 0~100 그래프 위치, 방향을 제공한다.
`interpretation`은 수치를 반복하지 않고 해당 비교가 왜 판단에 유효한지 설명한다. 큰 headline은
사용자 결론을 우선하고 숫자는 근거 그래프에 보조 정보로 남긴다. 그래프 위치는 백엔드가 확정하고
프런트는 투자 판단을 재계산하지 않는다.
`cautions[]`는 실패 조건, 알려진 soft penalty·실행 경고, 판단 유효 범위와 신뢰도 의미를
`code`, `label`, `severity`, `sentence`로 제공한다. 이 문장 계약은 가격·점수 계산을 변경하지 않는다.
decision v1 flag가 꺼진 응답은 decision/sizing/keyEvidence/counterEvidence/cautions를 제거해 구형
action 값이 직접 매수 권한으로 오인되지 않게 한다.
이 경로는 DB run/item 저장, 알림 발행, 개인화 학습을 수행하지 않고 worker도
`fixed_replay_override` 상태만 반환한다. manifest/file/recommendation digest가 하나라도
맞지 않으면 legacy나 LIVE 결과로 fallback하지 않고 503을 반환한다.

SIM middleware는 이 검증된 provider가 준비된 추천 경로만 예외적으로 허용한다. 따라서
같은 사용자의 LIVE와 SIM은 같은 recommendation API와 byte-equivalent item·digest를
사용한다. override가 꺼져 있으면 기존처럼 point-in-time 추천 경로를 409로 차단한다.

V2 commit은 사용자 advisory lock 아래에서 slot idempotency와 예상 preference state를
재확인하고, processed/skipped events, immutable preference/risk states, 모든 적격 후보의
feature evidence, Top 15 item, run provenance와 digest를 한 transaction에 저장한다. 완료된
slot은 이후 체결로 덮어쓰지 않으며 state 충돌은 최신 context로 한 번만 재계산한다.
응답은 기존 필드를 유지하면서 `algorithmVersion`, `extendedBaseAlphaScore`, fundamental,
preference, `effectiveWeights`, `personalizationDelta`, `riskBudget`, `observedRisk`를 optional로
추가한다. 펀더멘털 provider가 없거나 validation에 실패한 종목은 제외하지 않고 9팩터로
재정규화한다.

`POST /api/agents/analyze`는 로그인 session cookie로 사용자를 확인하고 다음 header만
클라이언트 입력으로 읽는다.

```text
Idempotency-Key
```

`X-GOPS-User-Id`와 body의 `userId`는 신뢰하지 않는다. 백엔드는
`Depends(require_current_user)`가 반환한 session `sub`를 canonical user id로
사용한다. `Idempotency-Key`가 있으면 같은 session user/key 조합은 같은
`analysisId`를 재사용해야 한다. 이미 완료된 report가 있으면 completed report를
반환할 수 있다.

인증이 활성화된 환경에서는 analyze와 layout resolve를 session 사용자별 기본
30회/60초로 제한한다. rate-limit Redis를 확인할 수 없으면 제한을 우회하지 않고
`503`을 반환하며, 한도를 넘으면 `Retry-After`와 함께 `429`를 반환한다.

`POST /api/agents/layout/resolve`는 UI-only layout command preflight다. 이 route는
Kafka queue와 report polling을 타지 않고 `agent-orchestrator`의 `/layout/resolve`
compat endpoint를 동기로 호출한다. 응답은 다음 shape를 유지한다.

```json
{
  "status": "ui_layout",
  "summary": "변경했습니다.",
  "rationale": "시장 뉴스 패널 크기를 조정했습니다.",
  "analysisId": "agent-request-id",
  "route": {"source": "ui-parser", "intentType": "ui-layout", "selectedRoles": []},
  "layoutProposal": {},
  "agentTrace": {"uiLayoutFastAck": true}
}
```

`status="not_ui"`이면 프런트는 기존 `POST /api/agents/analyze`로 fallback한다.
`status="ui_clarify"`이면 UI 관련 표현은 감지했지만 실행 가능한 layout task가
확정되지 않은 상태다. 프런트는 분석 fallback을 하지 않고 `summary`를 사용자에게
표시한다.

UI layout parser는 `패턴 종목`, `패턴 종목 목록`, `패턴 리스트`,
`패턴 보유 종목`을 `panelType="chartPatternList"`로 해석한다. 프런트가
`layoutContext.panelCatalog`를 보내지 못한 경우에도 기본 크기는 `2x2`, 제목은
`패턴 종목`으로 해석해 `layout.panel.add` proposal을 만들 수 있어야 한다.

## Request Shape

요청 body는 프런트와 차트 엔진이 context를 추가할 수 있도록 확장 필드를
허용하되 raw HTTP body와 검증된 전체 JSON을 각각 64 KiB 이하로 제한한다. raw
body 초과는 JSON 파싱 전에 `413`을 반환한다. `messages`는 최대 50개,
`references`와 `marketEvents`는 각각 최대 100개다. 문자열과 ID에도 필드별 길이
제한을 적용한다.

```json
{
  "symbol": "NVDA",
  "intent": "analysis",
  "routerMode": "hybrid",
  "messages": [{"role": "user", "content": "NVDA 분석해줘"}],
  "chartContext": {
    "chartDocument": {"symbol": "NVDA", "timeframe": "1D", "sourcePanelId": "chart-1"},
    "analysisWindow": {"viewportStart": "...", "viewportEnd": "...", "preRollCandles": 120},
    "assetIdentity": {"algorithmVersion": "...", "inputDigest": "...", "asOf": "..."}
  },
  "layoutContext": {},
  "references": [],
  "uiContext": {},
  "chartAction": null,
  "chartTargetSymbol": null,
  "chartPlacementIntent": null,
  "requestId": "agent-request-client-id",
  "mode": null,
  "analysisMode": null,
  "priority": null,
  "responseMode": null
}
```

To request the coach report, the frontend adds:

```json
{
  "coachRequest": {
    "enabled": true,
    "selectedFillId": null,
    "tradingDate": "2026-07-14"
  }
}
```

The request body never carries fills, positions, portfolio snapshots, indicators, or
historical cases. These are server-owned inputs and are too large and too sensitive for
the public 64 KiB request contract.

백엔드는 모르는 agent context field를 worker envelope로 전달하되 `userId`,
`idempotencyKey`, `submittedAt`, `maxLlmCalls`, `maxInputTokens`,
`maxOutputTokens`, `llmBudgetOwner` 같은 서버 소유 필드는 제거한다.

`intent="analysis"` 또는 `intent="analyze"`는 placeholder다. runtime은 `prompt`,
그 다음 최신 user message를 실제 intent로 사용한다. 종목은 명시적 ticker/정확한 회사명,
chart reference, 활성 chart document, request symbol, 제한된 fuzzy 순으로 결정하며
유효한 chart context를 fuzzy 후보가 덮어쓸 수 없다.

`references`, `uiContext`, `chartContext`의 선택/hover reference는 worker payload에
보존되어 agent runtime의 `OperationIR` extractor로 전달된다. runtime은 같은
reference가 여러 필드에 중복 포함돼도 fingerprint로 dedupe한다. reference가 포함된
분석 요청은 선택한 뉴스, 차트 봉, 차트 구간이 cache key에 반영되어야 하며, 같은
자연어 질문이라도 anchor가 다르면 cached analysis를 재사용하지 않는다.
`chart.orderFlow`, `chart.pattern`, `chart.drawing` references are valid chart references
and must be preserved the same way as `chart.candle`; their bid/ask side classification is estimated,
not provider-confirmed.

완료 report는 additive `chartExplanation`을 포함할 수 있다. `version`은
`chart-explanation.v1`이며 `quality`, `facts`, `usedIndicators`, `focusIds`, `anchor`,
`news`를 담는다. optional `source`는 요청의 `chartDocumentId/sourcePanelId`를 echo하고,
optional `focusGroups`는 기존 `focusIds` 합집합을 evidence/pattern/support/resistance로
분류하며 Geometry v6이면 optional levels/trend 그룹과 trend fact를 함께 보존할 수 있다.
기존 required facts/groups는 변하지 않으며 이 optional 필드가 없는 기존 v1 응답도
유효하다. `finalAnswer`가 사용자 문장 계약이고
`chartExplanation`은 UI의 구조화 렌더링 및 요청 시점 drawing focus snapshot 계약이다.
현재 자산과 `symbol/interval/assetVersion/algorithmVersion/inputDigest/asOf`가 정확히
일치하지 않으면 프런트는 서버 수치만 표시하고 현재 작도를 focus하지 않는다.
provider/snapshot/LLM fallback과
`investment_advice_limited` 같은 내부 정책 코드는 trace에만 둔다.

완료 report의 `timing`은 synthesis 진단을 포함할 수 있다.
`llmCallLabels`에 `synthesis` 또는 `financial-synthesis`가 있으면 최종 종합 답변용
LLM 호출을 획득/시도한 것이다. 최종 답변이 실제 OpenAI 결과인지 여부는
`synthesisProvider="openai"`로 판단한다. `synthesisSkippedReason` 또는
`synthesisFallbackReason`이 있으면 deterministic fallback 답변으로 degrade된 것이다.

## Entity Resolve Shortcut

`GET /api/agents/entities/resolve?q=...&mode=chartShortcut`는 에이전트
카탈로그의 `KoreanEntityResolver`를 얇게 노출한다. 프런트는 에이전트 입력이
회사명/티커 단독이거나 `애플차트 보여줘`, `AAPL chart` 같은 chart-open 명령인지
확인해 차트만 즉시 전환할 때 이 route를 쓴다.

응답의 `chartShortcut=true`는 입력이 company/ticker로 확정되고, 나머지 표현이
차트 열기/전환/추가 명령일 때만 반환한다. 해당 symbol은 기존 market-data
symbol registry/universe에서 차트로 열 수 있어야 한다. 다중 차트 요청이면
호환용 `symbol`은 첫 번째 종목을 담고 optional `symbols`가 입력 순서의 ticker
목록을 담는다. `엔비디아 뉴스`,
`엔비디아 분석해줘`, `엔비디아 차트 분석해줘`, 관계 질문, 테마 entity,
ambiguous match, registry 미지원 symbol은 분석 요청으로 fallback할 수 있도록
`chartShortcut=false`를 반환한다.

응답은 optional `chartAction`을 포함할 수 있다. 기본 `애플 차트 보여줘`,
`AAPL chart`, 회사명/티커 단독 입력은 `chartAction="replace"`로 현재 chart
symbol 전환을 의미한다. `애플 차트 추가해줘`, `애플도 같이 보여줘`,
`AAPL chart too`, `애플 차트 엔비디아 차트 같이 보여줘` 같은 비교/동시 표시
표현은 `chartAction="add"`로 기존 chart를 유지한 추가 chart panel 요청을
의미한다. 위치 표현이 있으면
`chartPlacementIntent`에 `top`, `bottom`, `left`, `right`, `center` 중 하나를
담는다.

프런트가 chart add shortcut을 `POST /api/agents/layout/resolve`로 넘길 때는
`chartAction="add"`, `chartTargetSymbol`, `chartPlacementIntent`를 request body에
포함할 수 있다. 백엔드는 이 필드를 제거하지 않고 orchestrator로 전달해야 한다.
orchestrator는 이 메타데이터를 UI-only layout task로 보강해 analysis pipeline을
타지 않고 `layout.panel.add`와 priority-aware `layout.panels.arrange` proposal을
반환한다.

이 route는 `gops-backend` process 안에서 `KoreanEntityResolver`를 직접 실행한다.
따라서 backend image에도 `systems/agent-orchestration/config`와
`systems/agent-orchestration/shared`가 함께 포함되어야 한다. 운영 alias catalog인
`entity-aliases.json`이 없으면 bootstrap seed로 degrade해 seed에 없는 회사명
shortcut을 놓칠 수 있다.

## Async Submit Response

Async submit은 기본적으로 `202`를 반환한다.

```json
{
  "request_id": "agent-request-id",
  "analysisId": "agent-request-id",
  "status": "queued",
  "status_url": "/api/agents/reports/agent-request-id",
  "stream_url": "/api/agents/reports/agent-request-id/stream",
  "report": {}
}
```

완료된 idempotent retry는 `200`과 completed `AnalysisReport`를 반환할 수
있다. 프런트는 `analysisId`를 canonical key로 사용한다.

## Cancellation

`POST /api/agents/reports/{analysis_id}/cancel`은 사용자가 실행 중인 분석을
중단할 때 호출한다. 프런트가 submit 전에 `requestId`를 생성해 body에 넣으면
submit 응답이 오기 전에도 같은 값을 `analysis_id`로 cancel할 수 있다.

백엔드는 submit 시 `agent:report:owner:{analysisId}`에 session user의 hash를
저장한다. report 조회, cancel, SSE stream은 owner가 일치하지 않으면 존재 여부를
노출하지 않도록 `404`를 반환한다. client request id 충돌은 다른 사용자의 owner
mapping을 덮어쓰지 않고 `409`로 거절한다.

Cancel은 cooperative cancellation이다. 백엔드는 report store에 `canceled`
terminal report와 cancel marker를 저장하고 Redis update channel로 publish한다.
queued Kafka message는 삭제하지 않으며, worker는 message를 소비할 때 marker를
확인해 orchestrator 실행을 건너뛴다. 이미 실행 중인 worker/orchestrator는 단계
경계에서 marker를 확인하고 `completed` 결과가 `canceled` report를 덮어쓰지
못하게 해야 한다.

이미 `completed`, `deep_completed`, `failed`인 report는 cancel로 덮어쓰지 않는다.
프런트는 `completed`, `deep_completed`, `failed`, `canceled`를 terminal status로
취급한다.

## Queue And Report Store

기본 production path:

```text
API acknowledgement
-> Kafka agents.analysis-requests.v1
-> agent-analysis-worker
-> Redis report store
-> Kafka agents.analysis-results.v1
-> agent-delivery-gateway
-> Redis pubsub
-> polling or SSE delivery
```

필수 Redis keys/channels:

```text
agent:report:{analysisId}
agent:report:latest:{SYMBOL}
agent:report:latest
agent:request:idempotency:{userHash}:{keyHash}
agent:report:cancel:{analysisId}
agent:report:owner:{analysisId}
agent.reports
agent.reports:{analysisId}
```

Report persistence failure는 가능하면 analysis generation을 막지 않도록 fail
open한다. 단, polling/SSE 품질은 Redis store 상태에 의존한다.

AWS overlays explicitly set `AGENT_ANALYSIS_QUEUE_BACKEND=kafka` and
`AGENT_REPORT_STORE_BACKEND=redis`. Explicit backends fail closed during initialization;
process-local queue/report fallback is reserved for local `auto` configuration.

## Compatibility Mode

`AGENT_ASYNC_ANALYSIS_ENABLED=false`이면 백엔드는 Kafka enqueue 대신
`AGENT_ORCHESTRATOR_URL`의 HTTP endpoint를 호출할 수 있다.

`AGENT_SYNC_COMPAT_WAIT_ENABLED=true`는 queue-backed path를 유지하면서 짧게
Redis report store를 wait하는 테스트용 compatibility mode다. 운영 기본값으로
쓰지 않는다.

## Required Env

Backend/API side:

```text
AGENT_ORCHESTRATOR_URL
AGENT_ASYNC_ANALYSIS_ENABLED
AGENT_SYNC_COMPAT_WAIT_ENABLED
AGENT_SYNC_COMPAT_WAIT_TIMEOUT_SECONDS
AGENT_SYNC_COMPAT_WAIT_POLL_SECONDS
AGENT_SHARED_REPORT_STORE_ENABLED
AGENT_ANALYSIS_QUEUE_BACKEND
AGENT_REPORT_STORE_BACKEND
AGENT_REPORT_TTL_SECONDS
AGENT_REPORT_CANCEL_KEY_PREFIX
AGENT_REPORT_OWNER_KEY_PREFIX
AGENT_RATE_LIMIT_ENABLED
AGENT_RATE_LIMIT_REQUESTS
AGENT_RATE_LIMIT_WINDOW_SECONDS
AGENT_REPORT_STREAM_REDIS_ENABLED
AGENT_REPORT_UPDATES_CHANNEL
AGENT_IDEMPOTENCY_TTL_SECONDS
AGENT_IDEMPOTENCY_KEY_PREFIX
AGENT_ENTITY_ALIAS_CATALOG_PATH
AGENT_ENTITY_ALIAS_SEED_PATH
AGENT_ENTITY_CATALOG_STRICT
AGENT_MARKET_SYMBOL_REGISTRY_PATH
KAFKA_BOOTSTRAP_SERVERS
AGENT_ANALYSIS_REQUESTS_TOPIC
AGENT_ANALYSIS_RESULTS_TOPIC
AGENT_DLQ_TOPIC
AGENT_OUTPUT_KAFKA_REQUIRED
REDIS_URL
```

Worker side:

```text
AGENT_DEEP_ANALYSIS_ENABLED
AGENT_DEEP_ANALYSIS_REQUESTS_TOPIC
AGENT_QUERY_UNDERSTANDING_EVENTS_TOPIC
AGENT_NOTIFICATION_DECISIONS_TOPIC
AGENT_MARKET_EVENTS_TOPIC
AGENT_PUBLISH_TO_KAFKA
CLICKHOUSE_HTTP_URL
CLICKHOUSE_DATABASE
CLICKHOUSE_USER
CLICKHOUSE_PASSWORD
GRAPHDB_SPARQL_URL
GRAPHDB_REPOSITORY
OPENAI_API_KEY
AGENT_OPERATION_PLANNER_PROVIDER
AGENT_OPERATION_PLANNER_MODEL
AGENT_OPERATION_PLANNER_TIMEOUT_SECONDS
DATABASE_HOST
DATABASE_PORT
DATABASE_NAME
DATABASE_USER
DATABASE_PASSWORD
AI_COACH_SNAPSHOT_ARCHIVE_ENABLED
AI_COACH_SNAPSHOT_ARCHIVE_REQUIRED
AI_COACH_SNAPSHOT_S3_BUCKET
AI_COACH_SNAPSHOT_S3_PREFIX
```

### Post-market AI coach report read

`GET /api/ai-coach/reports/latest` is an authenticated read-only endpoint for the coach
panel. It does not invoke `AgentOrchestrator`, Kafka, Redis, or a provider. The endpoint
derives the user prefix from the authenticated subject and reads only that user's S3
`latest.json` pointer and immutable daily `coach-report.v2`. It returns `{status:"ready",
report}` or `{status:"pending", report:null}`; S3 access failure is `503`.

The analysis worker reads the separate post-market input archive from
`ai-coach/input/v1/user={subjectHash}/date=YYYY-MM-DD.json`. The archive's required
`sourceAsOf`/`generatedAt` must not be later than the requested cutoff. No order route,
broker route, Redis cache, or client supplied snapshot is used to fill a missing archive.

`AGENT_OPERATION_PLANNER_PROVIDER=openai` enables the slow-path structured
OperationIR planner for low-confidence or ambiguous interactive requests. Keep it
unset to run only deterministic extraction.

## Company Compare M2/M3 bridge

Public backend는 `POST /api/llm/company-compare`에서 저장된 SEC/Yahoo 정량 자료와
10-K/GraphDB/news 정성 자료를 먼저 완성한 뒤
`AGENT_ORCHESTRATOR_URL/company-compare/narrative`에 그 payload만 전달한다.
agent-orchestrator가 OpenAI Responses API strict structured output을 실행하므로 LLM은
데이터 조회나 수치 계산을 하지 않는다. 8개 데이터 섹션이 활성화되면 schema도 동일한
8개 id를 빠짐없이 한 번씩 요구한다.

AWS에서는 ExternalSecret `/gops/prod/agent-orchestrator/openai/api-key`가
`alfaka-openai-secret.OPENAI_API_KEY`로 동기화되고 agent-orchestrator에 `envFrom`으로
주입된다. 값은 manifest나 repo에 기록하지 않는다. 키 누락, timeout, schema 위반은
public route 전체 실패가 아니라 `narrative.status=failed`와 정량-only 폴백으로 처리한다.

## Backend Reference Files

기존 구현을 참고할 때 볼 파일:

```text
systems/api-server/pods/api-server/gops-backend/app/contracts/agents.py
systems/api-server/pods/api-server/gops-backend/app/routes/agents.py
systems/api-server/pods/api-server/gops-backend/app/services/agent_gateway.py
systems/api-server/pods/api-server/gops-backend/app/services/agent_alert_payloads.py
systems/api-server/pods/api-server/gops-backend/app/main.py
systems/api-server/tests/test_agent_routes.py
```

새 백엔드는 구현을 바꿔도 되지만 route name, idempotency, async status,
polling/SSE semantics는 보존해야 한다.

## Chart Candle Runtime Contract

`POST /api/charts/active-symbol`은 `candles,trades,quotes` 레이어를 가진 제한된
realtime cohort를 먼저 갱신한다. 이어지는 `GET /api/charts/candles`는 Redis와
ClickHouse를 읽고, 최신 완료 NYSE 세션이나 현재 pre/after/overnight tail이
누락됐으면 동일 요청 범위만 Alpaca REST로 복구한다. Overnight 구간은 BOATS로
라우팅하며 과거 장외 봉도 응답에서 숨기지 않는다.

## Chart Analysis Asset Routes

Chart analysis asset은 interactive agent report와 별도인 수동 build projection이다.

```text
GET    /api/charts/analysis-assets
DELETE /api/charts/analysis-assets?symbols=NVDA&intervals=1D
GET    /api/charts/analysis-assets/coverage
POST   /api/charts/analysis-assets/build
GET    /api/charts/analysis-assets/build/{job_id}
POST   /api/charts/analysis-assets/build/{job_id}/cancel
```

DELETE는 개발 패널의 명시적 정리 기능이다. 최대 100개 symbol과
`1m/5m/10m/1h/4h/1D/1W`만 받고 선택된 pair를 PostgreSQL
`chart_assets.geometry_assets`에서 삭제한다. 자동 TTL이나 broad cleanup은 사용하지
않는다. 새 build 요청은 운영 interval `1m/1D`만 허용한다. 다른 지원 interval의 기존
자산은 GET/DELETE와 표시 호환을 위해 유지한다. build 완료·삭제 후 프런트는 cache를
무효화하고 열린 chart를 재조회한다.
Build 상태와 bounded log는 PostgreSQL polling 응답으로 제공한다. Redis pub/sub과
SSE route는 사용하지 않는다.
API에서 만든 수동 build는 서버가 `source=manual`, `priority=100`으로 지정하고 정기
build는 `source=scheduled`, `priority=10`으로 지정한다. 클라이언트는 priority를
보내지 않는다. 실행 중인 동일 source/force/symbol/interval 요청은
`request_fingerprint`로 기존 job에 합치며 응답의 `coalesced=true`로 알린다.
Worker는 `scheduled` item을 candle 조회·복구·분석·저장 전에 `manual_refresh_only`로
종료한다. 기존 자산은 선택된 pair의 `manual + force` 요청에서만 교체하며 일반 manual
요청은 없는 자산만 만들 수 있다. `symbols="sp500"`와 `force=true` 조합은 API가
400으로 거절하며 전체 universe force 갱신은 제공하지 않는다.
Worker는 높은 priority부터 claim하고, 최대 2회 처리 뒤 lease가 만료된 item은
`lease_expired_after_max_attempts` 실패로 종결해 job이 영구 대기하지 않게 한다.
최종 생성량은 status의 작은 `createdEntities` 정수만 사용한다. Coverage의
`drawingCount`는 저장된 엔티티 수이며 호환 alias `storedDrawingCount`와 같다. 실제
차트 적용 수와 anchor/stale 제외 수는 현재 candle과 active chart document의 실제
drawing ID를 아는 프런트가 계산한다.

Geometry v6 payload는 기존 패턴 필드와 함께 optional `trends`, `primaryTrend`,
`drawingGroups`, `analysisTrace`를 `geometry` 아래에 가진다. 저장 drawing은 levels 4,
pattern 3, trend/channel 1의 합계 최대 8개이고 canonical UTF-8 JSON은 256 KiB 이하다.
trace v2는 detector의 ranked 후보와 접촉 episode를 생략하지 않고 detected/stored
completeness를 검증한다. 전체 payload가 초과하면 후보를 제거하지 않고 저장을
실패시켜 이전 row를 유지한다. v6는 기존 JSONB와 drawing-count check 안에서 동작하므로
chart-asset table/data migration을 다시 실행하지 않는다.

`CHART_ASSET_STORAGE_MAINTENANCE=true` 동안 GET은 계속 열어 두고 build와 DELETE만
503으로 막는다. 기존 숫자형 자산은 변환하거나 fallback으로 읽지 않는다.

SIM의 `GET /api/charts/analysis-assets?symbol=NVDA&interval=1m`은 저장 자산 중
`asOf <= virtualTime`인 것만 먼저 남긴다. 요청 interval은 replay 시작 전 ClickHouse
과거 봉과 simulator가 cursor까지 반환한 replay 봉을 기존 chart merge 규칙으로 합친 뒤,
완료 봉 120개 이상일 때 Geometry v6 분석을 동기 worker thread에서 한 번 실행해 응답의
해당 interval만 교체한다. 이 동적 자산은 PostgreSQL에 저장하거나 build queue에 넣지
않으며 `meta.simulation`, `cutoff`, `runId`, `dynamicInterval`, `dynamicStatus`를 함께
반환한다. 봉이 부족하거나 분석 입력을 읽지 못하면 미래 저장 자산으로 fallback하지
않고 각각 `data_insufficient` 또는 `unavailable` 상태와 안전한 과거 자산만 반환한다.
요청 interval이 없으면 동적 분석 없이 cursor-safe 저장 자산만 반환한다.

## AI Company Journal Routes

```text
GET /api/company-journal/{symbol}
GET /api/company-journal/{symbol}/evidence?benchmarks=SPY,SOXX
```

응답은 `status=ready`와 최신 verified report 또는 `status=pending`과 null report다.
GET은 먼저 ClickHouse의 저장 결과를 반환하고 FastAPI background task에서는 원천 digest와
생성 event만 기록한다. OpenAI 생성은 CronJob worker에서 수행한다. 결과가 없다는 이유로
브라우저 계산 문장이나 fixture를 production 응답에 넣지 않는다. 이 route는
`POST /api/agents/analyze`, polling/SSE, Redis report store 계약을 변경하지 않는다.

저장 테이블은 `company_journal_reports_v1`과 `company_journal_generation_events_v1`이며,
기존 원천 테이블의 행을 수정하거나 복제하지 않는다.
`company-journal.v2` report의 `tabs`는 `current/growth/profitability/earnings/stability/valuation`을
가진다. 입력 bundle은 ClickHouse의 최대 520개 종목/SPY 일봉과 최근 42개월 SEC 실제 실적,
Yahoo 예상 실적을 bounded 조회한다. Yahoo table이 아직 비어 있거나 선택적 원천 조회가 실패하면
route 자체를 실패시키지 않고 missing data로 남기며, 검증된 문장은 없는 숫자를 만들지 않는다.

`/evidence`는 기업저널 panel 전용 읽기 계약으로 분기 재무, SEC/Yahoo 실적, 최대 520개 일봉을
한 번에 반환한다. replay simulation 중 일반 시장/agent route의 point-in-time guard는 유지하고,
이 경로만 현재 기업저널의 저장 근거를 읽는다. 이 응답은 주문·추천·agent 입력으로 재사용하지 않는다.

## Failure Policy

- PostgreSQL enqueue 실패는 `202 queued`로 가장하면 안 된다.
- Redis report store가 없으면 polling/SSE는 degrade를 명시해야 한다.
- Provider no-data는 backend error가 아니다.
- `agent-analysis-worker` failure는 DLQ 또는 report status로 드러나야 한다.
- AWS에서 `AGENT_OUTPUT_KAFKA_REQUIRED=true`이면 completed result publish/flush
  실패를 성공 처리하거나 request offset을 commit하지 않는다. 로컬 기본값만
  `false`로 유지한다.
- API는 order/account/broker flow를 agent report 생성과 섞지 않는다.
- 가격 조건 command route는 완료 report를 읽기 전용 제안 원본으로만 사용하며,
  report 생성 route나 agent worker에서 주문을 제출하지 않는다.

## Validation

```sh
git diff --check
.venv/bin/python -m unittest discover -s systems/api-server/tests -p 'test_agent_routes.py'
.venv/bin/python -m unittest discover -s systems/agent-orchestration/tests -p 'test_*.py'
.venv/bin/python -m unittest discover -s systems/fundamentals/tests -p 'test_*.py'
.venv/bin/python -m pytest systems/api-server/tests/test_order_routes.py \
  systems/api-server/tests/test_paper_trading_routes.py
.venv/bin/python -m pytest systems/order/tests/kis_trader/unit
```

Runtime acceptance:

```text
POST /api/agents/analyze returns 202 and analysisId
agent-analysis-worker consumes agents.analysis-requests.v1
completed report is saved in Redis
GET /api/agents/reports/{analysis_id} returns the completed report
GET /api/agents/reports/{analysis_id}/stream emits updates or frontend can poll
```
