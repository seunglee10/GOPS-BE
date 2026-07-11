# 06. 빌드 파이프라인·저장·연산 효율

기존 독립 `chart-asset-builder`/Kafka/API/Redis 경계를 유지한다. orchestration agent
runtime에는 손대지 않는다.

## 1. 심볼 중심 빌드 흐름

v1은 interval마다 load→kernel→LLM→save를 반복한다. v2는 symbol당 공유 단계를 묶는다.

```text
for symbol in bounded thread pool:
  1. canonical daily source 1회 load
  2. requested 1M/1W/1D rows + coverage + interval inputDigest 생성
  3. interval별 preflight
  4. 최신 asset의 input/rule/context/intent/content digest와 version 읽기
  5. 모든 buildIntentDigest와 agentOutcome predicate가 같으면 fast no-op으로 symbol 종료
  6. 필요한 interval feature/candidate/rule layer와 ruleDigest 계산
  7. stored eligible higher-TF compact summary 결합 + contextDigest 계산
  8. 최종 buildIntentDigest와 agentOutcome predicate가 unchanged면 kernel 이후 no-op으로 종료
  9. llmEnabled이고 LLM 대상이 있으면 symbol MTF call 1회
 10. interval별 v2 asset 조립
 11. interval assetContentDigest가 바뀐 row만 ClickHouse save
 12. progress item publish
```

심볼 내부 단계는 순차·결정론적이고 심볼 사이만 기존 bounded concurrency로 병렬화한다.
5번 fast no-op은 interval inputDigest, kernel/quality/candle/assembler/prompt/model policy,
requested model, LLM mode, agent preservation source, 실제 higher-TF summary digest가 모두
persisted 값과 같고 02의 agentOutcome predicate를 만족할 때만 허용한다.
versioned pure kernel 계약상 이 조건이면 stored rule/context/build digest 재사용이 안전하다.
하나라도 증명할 수 없으면 6번부터 계산하고, 최종 intent digest가 같으면 8번에서 LLM과
INSERT를 생략한다.
curator를 실행한 경우 content digest는 10번 뒤에만 계산한다. 기존 content와 같으면 11번
INSERT와 frontend cache invalidation만 생략하며, 이미 실행 전인 LLM을 content digest로
건너뛰었다고 기록하지 않는다.

## 2. 부분 interval 요청

- `1D`만 요청하면 1D 계산에 필요한 daily range만 읽는다.
- higher-TF context는 같은 실행에서 계산한 값 → 저장된 eligible v2 compact summary 순서로
  사용한다.
- 저장 summary는 candle/kernel/quality version 호환, `summary.asOf <= target.asOf`, target
  시점의 expected completed bucket freshness를 모두 통과할 때만 쓴다. historical eval에
  현재 summary를 넣어 미래 정보를 누설하지 않는다.
- v1 higher-TF prose나 unverified feature를 v2 context로 쓰지 않는다.
- higher-TF가 없으면 `no_higher_tf_context`를 기록하되 current interval hard gate 결과는
  유지한다.
- 데이터 insufficient interval은 LLM bundle에서 제외한다.

## 3. `llmEnabled=false` 의미

rule-only build는 v2 deterministic S/T와 rich fallback commentary를 만든다.

- 새 asset: I는 빈 정상 layer, `emptyReason="llm_not_requested"`,
  `agentOutcome="not_requested_empty"`, top-level은 데이터가 eligible이면 `ready`다.
- 기존 I를 보존할 수 있는 유일한 경우: input digest, kernel/quality/prompt version,
  candidate IDs가 모두 동일하고 기존 v2 I가 validator를 다시 통과함.
- digest나 version이 바뀌면 오래된 I를 보존하지 않는다. I를 비우고 fallback을 쓴다.
- v1 agent layer는 v2 rule-only asset에 복사하지 않는다.
- 기존 I를 보존하면 `agentOutcome="preserved"`와 source agent content digest를 intent에 넣는다.

이 변경은 잘못된 과거 geometry가 새 S/T와 섞이는 것을 막기 위한 v2 명시 계약이다.

## 4. 두 단계 no-op

### Age fast gate

기존 wire field `skipFreshHours`는 유지한다. API/worker/env 이름을 바꾸지 않는다.
시간 조건이 충족되면 candle query 전 빠르게 skip할 수 있다.

### Digest gate

age gate를 통과하지 않았어도 02의 `buildIntentDigest`와 agent outcome이 no-op 적격이면
다음을 모두 생략한다.

```text
kernel compute
LLM call
ClickHouse write
frontend cache invalidation 대상 표시
```

prompt/model selection version, LLM mode, MTF context가 바뀌면 `buildIntentDigest`도 바뀐다. 전체
candidate palette를 저장하지 않으므로 이 경우 kernel candidate부터 재계산한다. 저장 공간을
늘리는 palette cache 최적화는 v2 범위에서 하지 않는다.

`llmEnabled=true`인데 기존 agent outcome이 degraded면 age gate도 LLM recovery를 막지 않는다.
rule data가 같아도 candidate를 재생성해 실제 curator를 다시 호출한다. `llmEnabled=false`는
06 §3의 보존 정책으로 `rule_only + preserved|not_requested_empty` intent를 구분한다.

개발 패널의 사용자 문구는 07에서 `갱신 스킵(시간)`으로 바꾸지만 API field는 그대로다.

## 5. Bounded compute

- 1D query/normalization을 symbol당 공유
- pivot high/low 상위 K=12, pair side당 최대 66
- level cluster/touch episode는 정렬 후 bounded scan
- event state는 O(B × selectedLevels), selectedLevels 상한 8
- LLM top candidates interval당 6, symbol당 12
- JSON stable serialization과 digest 한 번
- threshold config를 module constant/versioned dataclass로 모아 hidden magic number 제거

새 SciPy/scikit-learn/pandas 의존성을 추가하지 않는다. Python 표준 라이브러리와 현재
repository dependency만 쓴다.

## 6. Compact asset

현재 NVDA v1 1D payload는 약 19.8KB였고 prompt input은 전체 feature 때문에 약 11KB였다.
v2는 전체 candidate universe를 저장하지 않는다.

저장할 것:

- input digest/policy/coverage
- regime compact summary
- 최종 drawing과 selected audit
- selected drawing/commentary가 참조한 pivots/levels/events/conditions
- higher-TF compact summary
- reject reason별 count, 전체 candidate/pass count
- model/prompt/usage/latency 요약

저장하지 않을 것:

- 전체 rejected candidate와 raw score ledger
- raw candle array
- raw prompt/response
- 전체 unreferenced pivot/event 배열
- LLM chain-of-thought 또는 hidden reasoning

v2 `features` 호환 필드는 bounded evidence view다.

| 항목 | hard cap |
| --- | ---: |
| pivots | 16 |
| levels/zones | 6 |
| trend summaries | 2 |
| events | 6 |
| fib candidates | 3 |

목표: payload p95 ≤ 12KB, hard cap 20KB. hard cap 초과 시 선택 근거를 자르지 않고 build
contract error로 드러내 원인을 고친다.

## 7. ClickHouse

기존 `market_data.chart_analysis_assets` ReplacingMergeTree와 `(symbol, interval)` overwrite를
유지한다. 신규 table/column/TTL/history를 만들지 않는다.

- `asset_version="v2"`와 JSON payload만 변경
- `argMax(payload, inserted_at)` read 유지
- unchanged `assetContentDigest`는 insert하지 않음
- canary symbol만 먼저 overwrite
- coverage API는 optional `assetVersion`, quality state, payload bytes를 additive하게 제공

DDL 변경이 없더라도 두 사본 parity checker를 매 묶음 실행한다.

## 8. Redis와 Kafka

Redis 허용 범위는 그대로다.

```text
gops:chart-assets:build:{jobId}
chart-assets.build:{jobId}
```

자산/후보/digest cache를 Redis에 넣지 않는다. Kafka topic/key/group도 유지한다.

```text
topic agents.chart-asset-build-requests.v1
group gops-chart-asset-builder
job one message
```

v2 요청 필드를 추가해야 하면 envelope의 optional `qualityPolicyVersion`/`force` 정도만
허용하고 기존 producer/consumer를 깨지 않는다. 자동 topic·CronJob은 추가하지 않는다.

## 9. 진행 상태

LLM은 optional enrichment이므로 다음을 분리한다.

| item status | 의미 |
| --- | --- |
| `saved` | 요청 단계 모두 성공 |
| `unchanged` | digest/version 동일, write 없음 |
| `skipped` | age gate/cancel/data-preserve 정책 |
| `saved_with_warning` | S/T 저장, LLM degraded 또는 optional source no-data |
| `failed` | candle/kernel/contract/storage 실패 |

job terminal:

```text
completed
completed_with_warnings
completed_with_errors
failed
canceled
```

프런트 terminal set과 SSE/polling test를 같이 갱신한다. LLM key 없음만으로 failed item이나
재실행 목록에 넣지 않는다. data insufficient로 기존 asset을 보존한 항목은 warning과
복구 안내를 남긴다.

Redis 상태의 `logs` 200, `recentItems` 50, failedItems 현재 bounded job 계약은 유지한다.
data-preserve는 `status="skipped"`와 bounded warning reason을 함께 쓰며 새
`skipped_warning` enum을 만들지 않는다.

## 10. LLM 비용·보존

- 호출 수: 최대 1/symbol/build, interval 수와 무관
- prompt top-K와 no raw bars
- `store:false`, bounded output
- actual usage/latency만 asset/build metric에 기록
- same digest no call
- 전체 S&P 실행 전 예상 **symbol call 수**를 UI에 표시(기존 interval 곱셈 제거)

OpenAI Batch는 현재 manual SSE job semantics와 다르고 품질 디버깅 피드백이 늦다. v2 완료
범위가 아니며, 전체 universe 비용/throughput 지표가 필요성을 증명할 때 별도 설계한다.

## 11. Failure/rollback

- 한 interval contract failure는 해당 item 실패, 다른 symbol 계속
- symbol MTF LLM failure는 eligible interval 전부 deterministic fallback + warning
- storage failure만 Kafka message redelivery 대상으로 유지
- invalid envelope poison-pill 정책 유지
- v2 canary 중 이상이면 job 중단, 이전 image로 canary symbol만 v1 재빌드
- 기존 정상 v2 asset은 insufficient fresh input으로 덮어쓰지 않음

## 12. 운영·문서 갱신

구현과 함께 갱신:

- `platform/kafka/README.md`: topic은 동일, v2 symbol-bundle/상태 의미
- `platform/redis/README.md`: key/channel 동일, 새 status enum
- `platform/clickhouse/README.md`: v1/v2 payload와 no-op/무이력 rollout
- `docs/AGENT_ARCHITECTURE.md`: 독립 builder 설명을 v2 candidate curator로 최신화
- `docs/AGENT_AWS_BUILD.md`: 현재 누락된 chart-asset-builder runtime/env/secret/smoke
- `docs/AGENT_FRONTEND_INTEGRATION.md`: v2 anchor/commentary/ops panel 계약
- `docs/CHART_DATA_ARCHITECTURE.md`: persisted offline analysis asset이 request-derived data와
  별도라는 현재 실제 구조

## 13. 테스트

- symbol당 daily query 1회/LLM 1회
- partial interval/higher-TF source priority
- rule-only preserve 조건과 stale I 제거
- age gate/digest gate/kernel-only reuse
- unchanged write 0회
- payload cap과 bounded evidence refs
- LLM warning vs data/kernel/storage failure status
- Redis key/channel 추가 없음
- SSE/polling terminal enums
- poison message/redelivery/cancel 멱등
- v1/v2 mixed serving과 canary overwrite
