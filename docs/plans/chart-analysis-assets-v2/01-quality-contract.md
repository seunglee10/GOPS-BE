# 01. v2 품질·자산 계약

## 1. 호환 전략

public route는 유지한다.

```text
GET  /api/charts/analysis-assets?symbol=NVDA
GET  /api/charts/analysis-assets/coverage
POST /api/charts/analysis-assets/build
GET  /api/charts/analysis-assets/build/{jobId}
GET  /api/charts/analysis-assets/build/{jobId}/stream
POST /api/charts/analysis-assets/build/{jobId}/cancel
```

응답의 `assets.{1D|1W|1M}` 구조도 유지한다. 자산 본문은 `assetVersion`으로 분기한다.

- 현재 schema를 `chart-analysis-asset-v1.schema.json`으로 보존한다.
- 신규 `chart-analysis-asset-v2.schema.json`을 만든다.
- 기존 canonical 경로 `chart-analysis-asset.schema.json`은 v1/v2 discriminator union이 된다.
- builder는 v2만 새로 쓴다.
- API와 프런트는 rollout 동안 v1/v2를 모두 읽되, v1에는 기존 동작을 유지한다.
- v2 canary가 통과하기 전 전체 자산을 덮어쓰지 않는다.

ClickHouse 테이블은 이미 `asset_version`과 JSON `payload`를 가지므로 DDL 변경이
필요 없다. 새 조회용 column을 편의상 추가하지 않는다.

## 2. 품질 상태

기존 `status: ready|degraded`는 호환용 top-level 상태로 유지하고 의미를 좁힌다.

| 상태 | 의미 |
| --- | --- |
| `ready` | 데이터 preflight 통과. 유효 후보가 0개여도 정상이다. |
| `degraded` | 데이터 부족, LLM 실패, 계약 불일치 중 하나가 있음. rule layer 일부는 남을 수 있다. |

세부 원인은 신규 `quality`와 layer `emptyReason`으로 분리한다. 빈 layer를 degraded와
동일시하지 않는다.

```jsonc
{
  "assetVersion": "v2",
  "kernelVersion": "kernel-v2",
  "qualityPolicyVersion": "chart-quality-v1",
  "promptVersion": "prompt-v2",
  "modelPolicyVersion": "chart-asset-model-v1",
  "input": {
    "digest": "sha256:...",
    "canonicalDataVersion": "v2",
    "sessionPolicy": "us-equity-regular",
    "adjustmentPolicy": "split",
    "candleContractVersion": "analysis-candles-v1"
  },
  "build": {
    "ruleDigest": "sha256:...",
    "contextDigest": "sha256:...",
    "buildIntentDigest": "sha256:...",
    "assetContentDigest": "sha256:...",
    "llmMode": "curate",
    "agentPreservationPolicy": "preserve_valid_same_input",
    "agentOutcome": "ready",
    "requestedModel": "...",
    "resolvedModel": "..." // audit only; no-op key에는 넣지 않음
  },
  "quality": {
    "state": "eligible", // eligible|insufficient_data|stale_input|contract_error
    "score": 0.86,
    "reasons": ["recent_contiguous_history", "exact_anchor_membership"],
    "penalties": []
  }
}
```

`quality.score`는 데이터와 채택 후보의 결정론적 요약이다. LLM이 쓰거나 수정하지 않는다.

`build.agentOutcome` enum:

| 값 | 의미 |
| --- | --- |
| `not_requested_empty` | rule-only 요청, I 비움 |
| `preserved` | 동일 입력/정책의 검증된 기존 I를 rule-only 요청에서 보존 |
| `ready` | curator 성공, I 1개 이상 |
| `ready_empty` | curator 성공, 선택 0개 |
| `degraded` | 호출/응답/validator 실패, deterministic fallback 사용 |

`ready_empty`는 실패가 아니다. `degraded`는 같은 curate 요청의 digest no-op 적격이 아니며
다음 명시적 build에서 recovery를 시도한다.

## 3. Coverage 계약

v1 필드에 다음을 additive하게 확장한다.

```jsonc
{
  "coverage": {
    "expectedBars": 120,
    "actualBars": 119,
    "missingBars": 1,
    "coverageRatio": 0.9917,
    "recentContiguousBars": 78,
    "largestGapBars": 1,
    "lastExpectedClosedAt": "...",
    "lastActualClosedAt": "...",
    "renderable": true,
    "qualityFlags": []
  }
}
```

`expectedBars`는 lookback 정수와의 차이가 아니라 exchange-session/bucket 기준이다.
`missingBars`도 그 expected set과 실제 candle key 차이로 계산한다.

## 4. 후보와 표시 결과를 분리한다

전체 `VisualCandidate`는 builder 메모리에만 존재한다.

```text
VisualCandidate
  candidateId               # digest에 종속된 안정 ID
  interval
  semanticType              # level|zone|event|trend|channel|range|fib|time-window
  drawingTemplate           # tool + 완성 canonical anchors + style/label token
  evidenceRefs[]
  counterEvidenceRefs[]
  confirmationConditionRef
  invalidationConditionRef
  redundancyKey
  quality
    hardPass
    score
    rejectReasons[]
    touchEpisodes
    independentConfirmations
    ageBars
    spanBars
    lastTouchAgeBars
    currentDistanceAtr
    residualAtr
    violationCount
    mtfConfluence
```

규칙:

- `hardPass=false` 후보는 ranking·LLM 입력·저장 대상이 아니다.
- score는 hard gate 통과 후보의 상대 순위를 정할 뿐, hard failure를 상쇄하지 못한다.
- `candidateId`는 `input.digest + interval + semanticType + evidence IDs + algorithm version`
  으로 결정론적으로 만든다.
- LLM은 `candidateId`만 선택한다. drawing template을 새로 만들 수 없다.
- 전체 후보와 reject ledger를 ClickHouse에 저장하지 않는다. 운영 통계는 reason별 count만
  잡 상태/로그에 남긴다.

## 5. 선택된 drawing 감사 정보

v2 layer는 기존 `drawings`를 유지하고 선택 근거를 bounded metadata로 추가한다.

```jsonc
{
  "layers": {
    "trend": {
      "drawings": [],
      "selected": [],
      "emptyReason": "no_candidate_passed_current_relevance",
      "meta": {"candidateCount": 3, "passedCount": 0, "rejectedByReason": {"stale": 2, "two_point_only": 1}}
    }
  }
}
```

`selected[]`에는 candidate ID, drawing IDs, evidence refs, quality summary만 저장한다.
원시 후보 배열이나 전체 pivot/event universe는 저장하지 않는다.

## 6. 시각 예산

예산은 “채우는 목표”가 아니라 통과 후보가 많을 때의 상한이다.

| 종류 | 상한 |
| --- | ---: |
| S H-Line/price zone | 3 |
| S Flag | 2, 단 총 S drawing은 4 이하 |
| T trend/channel/range | 1 |
| I insight | 2, 두 번째는 서로 다른 evidence·redundancyKey일 때만 |
| interval 전체 foreground | 5 |
| 넓은 fill zone | 1 |

어느 layer든 0개가 가능하다. draw coverage나 layer 채움률은 성공 지표로 쓰지 않는다.

규범 JSON layer key는 기존 계약대로 `structure`, `trend`, `agent`다. 문서의 S/T/I와 UI의
`구조/추세/인사이트`는 설명·표시 이름이며 `insight`라는 새 payload key를 만들지 않는다.
`no_draw`라는 상태 enum도 만들지 않는다. 정상 무작도는 top-level `status="ready"`,
`quality.state="eligible"`, 빈 `drawings`, 구체적인 `emptyReason`의 조합이다.

## 7. DrawingEntity 규범

기존 `shared/chart-contract/chart-command.schema.json`을 계속 따른다.

- ID: `ca-{symbol}-{interval}-{layer}-{candidateSuffix}`. 순번만 쓰지 않아 동일 digest에서 안정적이다.
- `sourceProposalId`, `createdBy`, `sourceInterval`, `historyScope` 적용 규칙은 v1을 유지한다.
- timed anchor timestamp는 02의 source candle timestamp와 exact match해야 한다.
- layer I도 geometry는 kernel이 만들므로 `createdBy`는 provenance를 위해 `llm`을 유지하되
  `selected[].geometryBy="kernel"`, `selected[].selectedBy="llm"`을 기록한다.
- trend extension은 candidate가 명시한다. active line만 `ray`, 역사적 설명 후보는 `segment`다.
- `riskRewardBox`는 계속 금지한다.

### H-Line label

H-Line label에는 숫자 가격을 넣지 않는다. 가격은 price-axis marker가 표시한다.

허용 예:

```text
지지
저항 · 매물대
월봉 저항
주봉 지지 · 매물대
```

rule/LLM 출처와 무관하게 compiler 마지막 단계에서 locale/소수점 반올림을 정규화한
**anchor 가격 token**이 H-Line label에 없음을 검증한다. `52주`처럼 가격이 아닌 의미
숫자는 제거하지 않는다. 가격은 commentary의 구조화된 key level에는 남긴다.

## 8. Commentary v2

기존 flat 필드는 하위 호환 렌더링용으로 유지하고 구조화 필드를 추가한다.

```jsonc
{
  "commentary": {
    "headline": "상승 구조는 유지되지만 최근 고점 아래에서 속도가 둔화됐습니다.",
    "regimeSummary": "...",
    "focusItems": [
      {
        "drawingIds": ["ca-NVDA-1D-trend-..."],
        "candidateId": "vc-...",
        "featureIds": ["p12", "p17", "p21"],
        "whatItShows": "최근 저점이 지지 추세를 재확인했습니다.",
        "whyItMatters": "현재 가격과 가까운 동적 기준입니다.",
        "whatToWatch": "다음 조정에서 종가 방어 여부를 보세요.",
        "confirmation": "cond-...",
        "invalidation": "cond-...",
        "horizon": "weeks"
      }
    ],
    "keyLevelsV2": [
      {"drawingId": "...", "role": "support", "price": 183.42, "reason": "최근 3회 방어·매물대"}
    ],
    "higherTimeframeContext": "...",
    "counterEvidence": ["..."],
    "dataCaveats": [],
    "confidenceV2": {
      "selection": {"score": 0.74, "reasons": ["..."], "penalties": []},
      "marketDirection": {"score": null, "reasons": [], "penalties": []}
    },

    "text": "호환용 조립 본문",
    "keyLevels": ["지지 183.42 · 최근 3회 방어·매물대"],
    "invalidation": "...",
    "confidence": 0.74,
    "enrichment": null
  }
}
```

불변식:

- 모든 displayed drawing ID는 정확히 하나 이상의 `focusItems`에 등장한다.
- 모든 `focusItems.drawingIds`는 실제 accepted drawing을 가리킨다.
- drop/reject 후보를 해설하지 않는다.
- `confidenceV2.selection`은 작도 선택/정상 빈 레이어 판단의 결정론적 품질이며 LLM self-score가
  아니다. `marketDirection`은 별도 근거가 있을 때만 채우고, 없으면 `null`이다.
- compatibility `text/keyLevels/invalidation/confidence`는 v2 구조에서 서버가 조립한다.
  flat `confidence`는 selection score로 매핑하고 UI는 이를 방향 확신으로 표기하지 않는다.

## 9. 버전과 rollback

다음 변경은 version bump 대상이다.

| 변경 | bump |
| --- | --- |
| 필드/의미 | `assetVersion` 또는 schema minor |
| pivot/level/trend/event 수식·threshold | `kernelVersion`, `qualityPolicyVersion` |
| LLM 입력/출력/지침 | `promptVersion` |
| 모델 선택·fallback 정책 | `modelPolicyVersion` |
| candle identity/aggregation | `candleContractVersion` |

rollback은 v1 row 복구가 아니라 **v2 canary 중단 + 이전 이미지로 선택 종목 재빌드**다.
ReplacingMergeTree의 무이력 원칙은 유지하므로 전체 rollout 전 canary가 필수다.
