# Chart Geometry Assets — Codex Reference

## 불변 조건

- 계산은 결정론적이고 point-in-time이며 ATR 정규화다. LLM, 난수, 미래 봉, 가짜 봉을
  사용하지 않는다.
- 입력은 정규장·분할조정·완료된 canonical candle이다. 모든 persisted timed anchor와
  confirmation은 asset `asOf` 이하의 실제 interval candle에 속해야 한다.
- 신규 생성·재생성 envelope는 `1m/1D`만 허용한다. 기존
  `5m/10m/1h/4h/1W` PostgreSQL row는 GET, 표시, 선택 DELETE 호환만 유지한다.
- 자산 저장은 PostgreSQL `chart_assets.geometry_assets` JSONB뿐이다. ClickHouse는
  canonical candle과 선택적 repair materialization만 소유하며 asset dual-write는 없다.
- `(symbol, interval)` 조건부 UPSERT와 `assetVersion="geometry"`를 유지한다.
  현재 `algorithmVersion`은 `ohlcv-consensus-pattern-families-v6`이다.
- 기존 row보다 과거 canonical `asOf`를 새 generatedAt만으로 덮어쓰지 않는다. 같은
  as-of의 v6 교체는 허용한다.
- 기존 패턴 detector, ranking, hardPass, confirmation, trade timing, primary 선택,
  drawing ID/anchor/label/style은 v5 golden과 같아야 한다.
- `drawings[]` 예산은 levels 4 + pattern 3 + trend/channel 1 = 최대 8이다. 그룹을
  부분 slice하지 않는다. 채널은 3-anchor `trendParallelLines` 하나다.
- canonical UTF-8 payload는 256 KiB 이하다. v2 complete trace가 초과하면 후보를
  제거하지 않고 저장을 실패시켜 기존 row를 유지한다.
- 신규 v6 필드는 `geometry` 아래 optional이다. DB table/column 삭제, payload 변환,
  일괄 backfill, Geometry v6용 migration Job은 실행하지 않는다.
- `tradePlan`, 해설, spotlight, trace overlay는 주문·알림 신뢰 원본이 아니다.

고정 시연 dataset의 `NVDA/1D`에는 명시적으로 제한된 build projection이 있다. 수동 SIM
build가 cutoff canonical 자산의 identity·coverage·indicator를 유지하면서 기존 하락 쐐기
geometry를 마지막 완료 봉 안으로 제한하고, buy-only 제안의
`simulation_demo_reward_risk_override` 사유를 남긴다. 이 geometry로 v5 commentary까지 만든
뒤 하나의 PostgreSQL snapshot으로만 저장한다. full/light identity가 일치하지 않거나 v5
commentary가 없으면 `ready`로 취급하지 않는다. 배포 전 구 snapshot의 runtime 호환 projection은
commentary를 반환하지 않고 `regeneration_required`를 표시하며, 새 snapshot 생성 후에는 적용되지 않는다.

## 계산과 자산 계약

`analyze_geometry()`는 `compute_pivots()`를 한 번 실행해 levels, trends, patterns에
같은 피벗 집합을 전달한다. `_pivot_evidence()`는 legacy evidence 호환용이며 v6 trace
원천이 아니다.

### 지지·저항

- confirmed: 기존 3 touch, 2 reaction, 최근성, 현재 관련성 게이트를 유지한다.
- contextual: 해당 role에 confirmed가 없을 때만 기존 3 touch, 1 reaction 보완을 허용한다.
- reference: confirmed/contextual이 모두 없을 때만 2 touch, 1 reaction, active/role-flip,
  role 방향, 최근성, 4 ATR 이내를 모두 요구한다.
- single swing, role conflict, break-pending 후보는 reference가 될 수 없다.
- role별 최대 2개이고 겹치는 zone은 tier, score, reaction, touch, recency, price, ID
  순으로 억제한다.
- importance는 첫 confirmed `major`, 두 번째 confirmed/contextual `standard`, reference
  `minor`다. 프런트 표시 스타일은 각각 `3/0.95/solid`, `2.25/0.82/[6,4]`,
  `1.5/0.62/[2,4]`다. importance가 없는 구자산은 기존 2.5px 표현이다.

### 추세

- `compute_trends()`의 structural pivot, 3 touch, 2 reaction, residual/span/relevance,
  invalidation, adverse-close 게이트를 유지한다.
- 최고 hard-pass 대각 후보 하나만 public trend로 선택한다. 없음은 정상 결과다.
- `GeometryTrend`는 ID/kind/direction/score/drawingId/anchors, pivot refs, touch/reaction,
  ATR/bar slope, residual, distance, recency와 채널 metrics를 저장한다.
- 일반선은 2-anchor `trendLine`, 채널은 3-anchor `trendParallelLines`와
  `parallelLineCount=2`다. 저장 표현은 2.75px, opacity 0.86, solid, ray이며 프런트
  presentation은 opacity 0.90을 적용한다.

### Trace

신규 writer의 `analysisTrace.version`은 `geometry-analysis-trace-v2`이고 v1은 읽기
호환이다. v2는 detector가 ranking에 전달한 후보와 touch episode 전체를 저장한다.
root `pivots[]`는 candidate의
`evidenceRefs/anchorPivotIds/touchPivotIds/reactionPivotIds` 합집합만 포함한다.
`touchRefs/reactionRefs`는 같은 candidate의 embedded `touches[].id`를 가리킨다.
패턴 접촉은 이미 같은 pivot registry에 있으므로 `touchPivotIds`로만 표현한다.

정렬은 selected를 먼저 두되 각 detector의 원래 deterministic ranking을 보존한다.
`disposition`, rank, selection/reject reasons와 render 계약을 저장하고 `completeness`의
detected/stored가 일치해야 한다. v2 성공 payload의 candidate omitted count는 0이다.

## 저장·빌드 경계

- `gops_agents.chart_assets.envelope`: build interval `1m/1D`, manual/scheduled source,
  server-owned priority와 request fingerprint
- `gops_agents.chart_assets.builder`: candle load/repair, kernel 조립, optional v6 passthrough,
  256 KiB fail-closed guard, bounded storage log
- `gops_agents.chart_assets.storage`: PostgreSQL-only conditional UPSERT, identity/v6 refs/
  drawing budget/payload size validation
- `gops_agents.chart_assets.job_store`: PostgreSQL queue, `FOR UPDATE SKIP LOCKED`, lease 2회
- `market_data.analytics.geometry`: public asset와 atomic drawing groups
- `market_data.analytics.levels|trends|patterns`: 후보 계산과 hard gates

일반 manual build는 없는 자산만 만들고 기존 row는 선택 pair의 `manual + force`만
교체한다. `scheduled` item은 candle 조회 전 `manual_refresh_only`로 종료한다.
`symbols="sp500" + force=true`는 API 400이다. 로컬 검증은 injected candle loader와
repair-disabled fixture만 사용하며 Alpaca credential이나 provider call이 필요하지 않다.
운영 builder 입력은 차트와 같은 Redis recent-closed + ClickHouse history canonical view다.
live candle은 제외하고 Alpaca repair는 ClickHouse에 실제 row를 materialize한 뒤 이 view를
다시 읽는다.

## 프런트 계약

레이어 키와 초기값은 다음과 같다.

```text
interpretation=false  해석
levels=true           저항 (aria/tooltip: 지지·저항)
trend=true            추세
pattern=true          패턴
proposal=false        제안
```

`drawingGroups`가 levels/trend/pattern 분류 원본이다. 구자산은 ID와 geometry metadata로
fallback 분류한다. SMA60/120과 cross는 trend가 소유한다. proposal hide는
`ActiveTradePlan`을 clear하지 않는다. interpretation은 persistent drawing이 아닌 Canvas
overlay이며 history/undo/export/8-drawing 예산에 들어가지 않는다. 글로벌 해석은 선별된
유력 미선택 후보와 확정 level/trend/pattern drawing 전체를 넓은 바탕선으로 표시하고,
해설 hover는 selections에 속한 후보의 marker만 표시한다. 바탕선은 그리드 위이면서
캔들·이평선·확정 drawing 아래에 둔다.
구자산은 후보를 합성하지 않고 v1은 일부 후보, legacy evidence는 근거만으로 명시한다.
토글의 진단 문구는 complete trace에서 현재 viewport 후보 수/전체 후보 수를 함께 표시한다.

자산 최신성은 `current|outdated_snapshot|source_invalid`다. outdated snapshot은 작도를
흐리게 하지 않고 N봉 전임을 표시하되 proposal을 stale로 둔다. canonical watermark
불일치나 stale input만 source-invalid로 dim한다.

해설은 규칙 기반 종합 해설, 주요 가격, 시나리오 뒤에 지지·저항, 추세, 패턴 판단 근거를
배치한다. metric은 접힌 상세 영역에 둔다. hover/focus는 해당 drawing만 강조하고 trace
subset의 pivot/touch/reaction marker를 표시한다. click은 한 섹션만 고정하고 다른 섹션
hover가 끝나면 고정 섹션으로 복귀한다. 대상 stroke/label은 opacity 1, 비대상 analysis
drawing은 0.45배, base chart는 0.60배다. fill opacity는 유지한다. 서버 metrics를
표시하며 프런트가 ATR/score를 재계산하지 않는다. 이번 구현은 desktop/tiled desktop만 대상으로 하고 mobile-specific
control, touch gesture, visual regression은 추가하지 않는다.

`chart-explanation.v1`은 기존 required fields를 유지하고 optional `facts.trend`,
`focusGroups.levels`, `focusGroups.trend`를 추가한다. `focusIds`와 기존
evidence/pattern/support/resistance group은 계속 호환된다.

## 검증

```sh
.venv/bin/python -m pytest systems/market-data/tests/analytics/test_geometry_assets.py
.venv/bin/python -m pytest systems/market-data/tests/analytics/test_patterns.py
.venv/bin/python -m pytest systems/market-data/tests/analytics/test_feature_pack.py
.venv/bin/python -m unittest systems.agent-orchestration.tests.test_chart_asset_builder
.venv/bin/python -m unittest systems.agent-orchestration.tests.test_chart_asset_storage
.venv/bin/python -m unittest systems.agent-orchestration.tests.test_geometry_asset_contract
.venv/bin/python -m unittest systems.api-server.tests.test_chart_assets_routes
```

프런트는 `apps/gops-frontend`에서 `npm run test:chart`, `npm run build`, desktop visual
spec을 실행한다. 모바일 viewport는 이번 acceptance 범위가 아니다.
