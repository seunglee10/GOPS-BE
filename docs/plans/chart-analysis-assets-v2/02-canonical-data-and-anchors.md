# 02. Canonical Candle · Coverage · Anchor 정렬

이 문서가 v2의 P0다. 입력 candle과 실제 렌더 candle이 다르면 이후 알고리즘이 아무리
좋아도 잘못된 선을 만든다.

## 1. 단일 분석 candle 경계

`systems/market-data/shared/alfaka/analytics/analysis_candles.py`를 신설한다.

```text
AnalysisCandleSource.load_symbol(symbol, requestedIntervals)
  -> raw canonical ClickHouse daily rows 1회 조회
  -> serving과 같은 session/adjustment/version 필터
  -> serving과 같은 market-day timestamp 정규화
  -> shared pure aggregation으로 1D/1W/1M 생성
  -> completed bucket 필터
  -> coverage + digest + rows 반환
```

원칙:

- worker는 REST API를 호출하지 않고 Redis도 읽지 않는다.
- 1D/1W/1M을 서로 다른 SQL 의미로 만들지 않는다. canonical 1D를 한 번 읽고 shared
  aggregator로 상위 주기를 만든다.
- 기존 API aggregation도 같은 pure function/parity fixture를 사용한다. API route shape와
  렌더 geometry는 바꾸지 않는다.
- `split + canonicalVersion=v2 + regular session + closed`만 사용한다.
- 1W/1M 진행 중 bucket은 제외한다.
- 같은 `(symbol, interval, candleKey)` correction/revision은 serving과 같은 winner를 고른다.

worker용 loader만 맞추는 것으로는 충분하지 않다. shared market-data 경계에 다음 pure
함수를 두고 기존 serving의 Redis/ClickHouse merge에도 재사용한다.

```text
canonicalize_candle_identity(row, interval, sessionPolicy)
choose_canonical_winner(rowsWithSameSymbolIntervalCandleKey)
merge_canonical_candles(redisRows, clickHouseRows)
```

- primary identity는 `(symbol, interval, candleKey)`다. timestamp는 identity를 canonical
  render timestamp로 표현한 값이다.
- API는 Redis-only, ClickHouse-only, 둘의 merge, pagination/gap-fill 모두 merge **전**에
  identity를 canonicalize하고 candleKey 중복 winner를 하나만 남긴다.
- WebSocket live candle의 기존 행동은 바꾸지 않는다. closed candle이 history로 편입되는
  경계와 reconnect gap-fill에 같은 identity/winner 함수를 적용한다.
- worker는 Redis를 읽지 않는다. ClickHouse의 마지막 closed key가 shared calendar의
  `lastExpectedClosedAt`보다 뒤처지면 stale preflight로 fail-closed한다.

winner policy는 호출 순서나 배열의 뒤쪽 값에 의존하지 않는다.

1. 요청한 canonicalVersion/session/adjustment와 맞지 않는 row를 먼저 제외한다.
2. `analysis_closed` view는 `isClosed=true`만 허용한다.
3. `chart_current` view에서 expected-closed key는 closed가 live보다 우선한다. 현재 active
   session key만 live를 허용하고, closed row가 도착하면 같은 key의 live를 대체한다.
4. 같은 source class 안에서는 correction/revision timestamp(`createdAt|updatedAt`) 최신,
   stable `sourceEventId` 순으로 고른다. source class는 현재 merge 의미를 고정한다.
   `chart_current`는 active live > Redis closed > ClickHouse direct > derived aggregate,
   `analysis_closed`는 ClickHouse direct > derived aggregate다.
5. 완전 동률이면 normalized payload hash lexical order로 하나를 고른다.

현재 direct/aggregate/Redis source 이름은 shared enum으로 매핑하고 기존 group precedence를
parity test로 보존한다. correction timestamp는 같은 source class 안의 revision을 고르는
값이며 live/closed 또는 source class 정책을 우회하지 않는다.

상위 주기만 요청한 경우 필요한 1D 범위만 읽는다. 세 주기를 모두 요청하면 symbol당
중복 query를 하지 않는다.

## 2. Timestamp와 candle key

각 row는 둘을 모두 가진다.

```jsonc
{
  "candleKey": "2026-07-02",           // 1D session date; 1W/1M은 bucket key
  "timestamp": "2026-07-02T04:00:00.000Z", // /api/charts/candles와 exact equality
  "barIndex": 117,
  "open": 190.0, "high": 200.06, "low": 189.2, "close": 199.3,
  "volume": 123,
  "isClosed": true
}
```

- `timestamp`가 canonical DrawingEntity anchor다.
- `candleKey`는 DST/표기 차이를 검증하는 identity다.
- `barIndex`는 해당 asset input 안의 계산용 index이며 저장 후 전역 index로 간주하지 않는다.
- ISO string 비교만 하지 않고 parse된 epoch와 candleKey parity도 테스트한다.

1D `00:00Z` 직접 집계가 뉴욕 시장일 `04/05:00Z` serving timestamp로 변환되는 fixture를
DST 전후에 고정한다. 1W/1M도 API와 builder의 bucket timestamp가 byte-identical해야 한다.

## 3. Exchange-session coverage

API server 안에만 있는 미국 주식 calendar 계산을 market-data shared pure module로
추출하고 API가 이를 재사용한다. 새 calendar dependency를 추가하지 않는다. 기존
holiday/env 정책과 결과가 바뀌지 않는 parity test를 먼저 만든다.

interval별 expected key:

- 1D: 정규장 session date
- 1W: 해당 주에 하나 이상의 정규장 session이 있는 completed week
- 1M: 해당 월에 하나 이상의 정규장 session이 있는 completed month

다음을 계산한다.

```text
coverageRatio          actual expected keys / expected keys
recentContiguousBars   asOf부터 뒤로 연속한 expected keys
largestGapBars         가장 긴 expected-key 결측 run
lastExpectedClosedAt   현재 시각 기준 마지막 닫힌 bucket
lastActualClosedAt     입력 마지막 bucket
```

초기 `chart-quality-v1` preflight 값은 다음과 같다. 08 real-data corpus에서 값 자체를
보정할 수 있지만 fail-open으로 완화하려면 근거와 version bump가 필요하다.

| interval | display coverage | recent contiguous | largest gap | stale 허용 |
| --- | ---: | ---: | ---: | ---: |
| 1D | ≥ 0.95 | ≥ 60 bars | ≤ 2 sessions | 0 expected closed bars |
| 1W | ≥ 0.92 | ≥ 26 bars | ≤ 1 bucket | 0 bucket |
| 1M | ≥ 0.90 | ≥ 18 bars | ≤ 1 bucket | 0 bucket |

### Capability별 fail-closed

| 실패 | 허용 산출물 |
| --- | --- |
| last bucket stale | directional drawings 없음, data caveat만 |
| recent contiguous 미달 | trend/channel/range/LLM visuals 없음 |
| display coverage 미달 | S/T/I 모두 없음. 기존 valid asset을 조용히 덮어쓰지 않음 |
| lookback 일부 부족, display 양호 | 최근 구조 허용, macro/history 점수 penalty |
| VP/order-flow coverage 없음 | 해당 confluence만 neutral, 가격 geometry는 계속 평가 |

데이터 부족은 LLM 장애와 별도 reason code로 기록한다.

## 4. 기존 자산 덮어쓰기 정책

새 입력이 preflight를 통과하지 못했을 때 정상 자산을 빈 degraded 자산으로 덮어쓰지 않는다.

```text
기존 v2 eligible asset 있음 -> 기존 asset 유지 + item status=skipped,
                                warning=input_insufficient_existing_asset_preserved
기존 asset 없음            -> degraded data-caveat asset 저장(작도 0)
기존 v1 asset만 있음       -> v1 유지 + 같은 warning
```

운영자가 데이터 복구 후 같은 요청을 재실행하면 v2를 생성한다.

보존된 row 자체에 새 경고를 덧씌우지 않는다. Redis에는 허용된 job item warning만 남긴다.
프런트는 이미 로드한 chart의 latest closed `candleKey`와 asset `asOf/candleKey`를 비교해
runtime freshness를 계산한다. 허용 stale 0 bucket을 넘으면 해당 자산 drawing을 적용하지
않고 `분석 자산 갱신 필요`를 표시한다. 개발 패널의 coverage API는 최신 closed key를
batched metadata query로 비교해 `freshness=current|stale|unknown`, `staleByBars`를 additive하게
반환한다. 자산 본문을 Redis에 복제하지 않는다.

## 5. Input digest

digest 입력:

```text
symbol, interval
candleContractVersion, canonicalDataVersion
sessionPolicy, adjustmentPolicy
ordered [candleKey,timestamp,OHLCV,isClosed]
optional confluence source digest/order-flow coverage
```

JSON은 stable key order와 정규화된 decimal/UTC로 직렬화해 SHA-256을 계산한다.

- interval `inputDigest`는 위 입력 candle/optional source만 나타낸다.
- `ruleDigest`는 inputDigest, kernelVersion, qualityPolicyVersion, 최종 bounded rule
  finding/candidate palette ID를 해시한다.
- symbol `contextDigest`는 실제 LLM bundle에 들어간 requested interval ruleDigest, 저장된
  higher-TF summary digest, cross-timeframe relation을 고정 순서로 해시한다.
- `buildIntentDigest`는 ruleDigest, contextDigest, promptVersion, modelPolicyVersion,
  **requested model**, asset assembler version, `llmMode=rule_only|curate`,
  `agentPreservationPolicy`, 보존 대상 agent content digest를 해시한다. API 응답 뒤에만 알 수
  있는 resolved model은 audit 필드이며 intent key에 넣지 않는다.
- `assetContentDigest`는 최종 status, selected candidate/fact 순서, drawing, indicator,
  commentary 구조화 필드, `agentOutcome`, resolved model을 stable serialization해 해시한다.
  generatedAt, job ID, latency, usage처럼 매 실행 달라지는 audit metric은 제외한다.
- 요청한 모든 interval에서 기존 `buildIntentDigest`가 같고 아래 agent outcome predicate를
  만족할 때만 symbol fast no-op다.

```text
requested curate   -> existing agentOutcome in {ready, ready_empty}
requested rule_only-> existing agentOutcome in {not_requested_empty, preserved}
degraded           -> 어떤 enabled 요청에서도 no-op 적격 아님
```

- `force=true`는 age/intent no-op을 우회해 평가하되 최종 content digest가 같으면 INSERT는
  하지 않고 `unchanged_after_force` reason을 남긴다.
- 일부 interval/context만 달라지면 바뀐 interval을 다시 조립하고, MTF context가 바뀐 interval의
  commentary/Layer I도 stale로 간주한다.
- 전체 candidate palette를 저장하지 않으므로 prompt/model만 바뀌어도 kernel candidate를
  재계산한다. 불완전한 selected evidence에서 후보를 복원하지 않는다.
- OHLCV correction이 하나라도 바뀌면 digest가 달라진다.
- digest cache는 ClickHouse 최신 asset을 사용하며 Redis에 저장하지 않는다.

## 6. Anchor 생성 hard invariant

모든 timed candidate를 materialize하기 전에 다음을 확인한다.

```text
anchor.timestamp in sourceCandles.timestamps
anchor.candleKey maps to exactly one source candle
anchor interval == asset interval
anchor time order satisfies tool contract
anchor price is finite and explained by source/evidence
```

Flag price 규범:

| event | y anchor |
| --- | --- |
| breakout/breakdown | signal close |
| retest | touched level/zone representative price |
| 52w high/low | candle high/low |
| gap | gap open |
| volume spike | candle close |

따라서 Flag dot이 wick 끝이 아닌 close에 있을 수는 있지만 x축은 반드시 해당 봉 중앙이다.

## 7. 프런트 적용 시 defensive resolve

asset 적용용 pure module `resolveAnalysisAssetAnchors(asset, candles)`를 둔다.

1. timestamp exact epoch match면 현재 candle timestamp/logicalIndex를 붙인다.
2. exact string만 다르고 같은 epoch면 현재 string으로 정규화한다.
3. legacy v1 또는 명시적 migration 대상 v2에서 같은 candleKey가 정확히 1개면 그 candle로 re-snap하고
   `canonicalized` telemetry를 남긴다.
4. 선택 anchor가 현재 loaded range보다 오래됐지만 asset `window` 안이면 기존 older-candle
   loader로 oldest anchor까지 bounded fetch한 뒤 적용을 defer한다.
5. loaded range 내부인데 매칭 candle이 없거나 후보가 fetch cap 밖이면 drawing 전체를
   drop하고 reason을 남긴다. 임의 nearest candle/continuous interpolation은 쓰지 않는다.

이 resolver는 `sourceProposalId`가 `chart-asset:`인 drawing에만 적용한다. 수동 future/
time-gap drawing의 현재 동작은 바꾸지 않는다.

canonical timestamp 전환이 기존 수동 anchor를 깨지 않게 chart time index에는 known candle
key별 legacy timestamp alias(예: 과거 1D `00:00Z`)를 둔다. alias는 실제 존재하는 candle의
lookup에만 쓰며 payload를 재작성하지 않는다. 임의 future/time-gap timestamp에는 적용하지
않는다. cursor, selection, drag, reconnect 후에도 같은 봉을 가리키는 fixture를 둔다.

## 8. Geometry parity

추세 후보 검증과 renderer가 같은 x 순서를 보도록 다음 fixture를 Python/TypeScript에서
공유한다.

- normal 1D sequence
- DST 전후 daily timestamp
- holiday와 missing session
- 1W/1M bucket
- viewport 밖 anchor + current projection
- semantic expansion on/off

fixture마다 `candleKey -> ordinal/slot`, anchor pixel 상대 순서, asOf projection price를
비교한다. backend에서 통과한 current relevance가 frontend render에서 뒤집히면 contract
failure로 처리한다.

자동 asset geometry의 규범 x축은 ordinal이다. resolver는 payload를 바꾸지 않고 내부
`ResolvedAssetGeometry` sidecar에 anchor slot을 붙인다. renderer의 semantic expansion
on/off 모두 이 slot을 사용한다. elapsed-time projection은 수동 drawing에만 유지한다.

offline hard gate의 price 범위는 사용자 viewport가 아니라 canonical display window의
`analysisPriceDomain = [min(low)-ATRpad, max(high)+ATRpad]`다. 초기 `ATRpad=1 currentATR`이며
quality policy version에 속한다. pan/zoom/auto-scale 상태는 kernel 입력이 아니다.

## 9. 테스트

- direct ClickHouse 1D timestamp와 `/api/charts/candles` timestamp parity
- Redis-only, ClickHouse-only, merged history, pagination, reconnect gap-fill identity parity
- active daily live → closed history identity 전환과 source/correction winner 순서
- legacy 00Z manual anchor/cursor가 canonical daily slot을 계속 가리킴
- 1D에서 만든 1W/1M과 serving aggregation parity
- 진행 중 주/월 bucket 제외
- DST, holiday, half-day, correction, duplicate row
- large recent gap, isolated gap, stale last bucket, short lookback
- digest determinism과 correction sensitivity
- 모든 timed anchor source membership
- v1 legacy anchor defensive canonicalization
- loaded-range missing anchor drop, older-range bounded defer
- 수동 drawing 좌표 동작 불변
