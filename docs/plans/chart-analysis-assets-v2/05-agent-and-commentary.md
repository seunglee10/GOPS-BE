# 05. Layer I 시각 큐레이터와 풍부한 해설

## 1. 역할 재정의

v1 Layer I는 `tool + anchorIds`를 만드는 작도 agent다. v2 Layer I는 **결정론적으로
검증된 visual candidate 중 무엇을 강조할지 고르는 시각 큐레이터**다.

LLM이 할 수 있는 것:

- 0~2개 candidate ID 선택
- 서로 다른 주기의 구조를 비교해 강조 순서 결정
- allowlist의 narrative fact·condition·counter-evidence ID를 골라 관찰 순서 정리
- 상위 주기 관계와 강조 방식 enum 선택

LLM이 할 수 없는 것:

- tool, style, color, anchor ID 조합 생성
- timestamp, price, slope, level, confidence 숫자 생성
- candidate hard gate 우회
- S/T drawing 복제
- 매수/매도·방향·목표가·손절가 선택
- `riskRewardBox` 또는 자유 `textLabel` 생성
- 근거가 없는 자유 factual clause 작성

풍부한 한글 문장은 LLM 원문을 그대로 표시하지 않는다. kernel/서버가 만든
`NarrativeFact`와 condition을 한국어 code-to-text로 조립하고, LLM은 **무엇을 어떤 순서로
강조할지**만 고른다. 이 경계가 좌표 환각뿐 아니라 “강한 반응” 같은 질적 환각도 막는다.

## 2. Layer I 후보 palette

kernel이 다음 semantic candidate를 완성 DrawingEntity template까지 만든다.

| 의미 | 도구 | 필수 추가 가치 |
| --- | --- | --- |
| 폭 있는 고품질 price zone | `horizontalParallelLines` | S의 H-Line보다 폭 자체가 중요 |
| 최근 consolidation base | `rangeBox` | 현재 break/retest 맥락과 연결. volume 근거 없이는 accumulation이라 부르지 않음 |
| 검증된 impulse retracement | `fibonacciRetracement` | 04 Fib gate 통과 |
| 두 사건 사이 event window | `verticalParallelLines` | 시간 구간 자체가 중요 |
| 중요한 단일 event | `flagMarker` | S와 중복되지 않고 현재 영향 지속 |
| 역사적 broken line/box | `trendLine`/`rangeBox` segment | 현재 event의 원 구조를 설명 |

`textLabel`은 해설 패널과 중복되므로 기본 후보에서 제외한다. H-Line과 active trend는
S/T가 담당하며 I가 다시 그리지 않는다.

candidate는 `allowedVisualTemplate` 하나를 가진다. LLM이 같은 의미를 다른 도구로
바꾸지 못한다.

각 type의 generator/hard gate/current relevance/redundancy/anchor 규칙은 Appendix A §9가
규범이다. 그 절에 없는 semantic type은 prompt palette에 넣지 않는다.

## 3. 심볼당 한 번의 멀티 타임프레임 호출

v1의 1M→1W→1D LLM 3회를 폐기한다.

```text
1. 요청 interval들의 canonical candle/feature를 결정론적으로 계산
2. 1M/1W/1D rule layer와 top candidates를 모두 준비
3. compact SymbolVisualBundle 생성
4. Responses API 1회
5. interval별 candidate 선택·해설 검증
6. 세 자산을 조립·저장
```

부분 interval build면 요청 interval과 저장된 eligible higher-TF compact summary만 bundle에
넣는다. 저장된 LLM 문장을 상위 주기 사실로 재주입하지 않는다. higher-TF context는
regime, selected levels/trend, evidence IDs, quality/counter-evidence만 사용한다.

## 4. Compact 입력

원시 20봉, 전체 pivot/level/event 배열을 보내지 않는다.

```jsonc
{
  "symbol": "NVDA",
  "symbolBundleDigest": "...",
  "intervals": [
    {
      "interval": "1D",
      "inputDigest": "...",
      "ruleDigest": "...",
      "asOf": "...",
      "quality": {"state": "eligible", "coverage": 0.99},
      "regime": {"trend": "up", "volatility": "normal", "momentum": "slowing"},
      "ruleFindings": [
        {"findingId": "1D:f-...", "drawingIds": ["..."], "evidenceRefs": ["1D:pivot:p12"], "factIds": ["1D:fact:7"]}
      ],
      "visualCandidates": [
        {
          "candidateId": "1D:vc-...",
          "semanticType": "retracement",
          "evidenceRefs": ["1D:pivot:p12", "1D:pivot:p17"],
          "counterEvidenceRefs": [],
          "factIds": ["1D:fact:11", "1D:fact:12"],
          "qualityBand": "high",
          "currentRelevance": "near",
          "confirmationConditionRef": "1D:cond:1",
          "invalidationConditionRef": "1D:cond:2"
        }
      ],
      "narrativeFacts": [
        {"factId": "1D:fact:7", "ownerRef": "1D:f-...", "clauseCode": "ACTIVE_SUPPORT_NEAR", "renderedKo": "최근 재확인된 지지 구간이 현재 가격과 가깝습니다."}
      ]
    }
  ],
  "crossTimeframe": {
    "alignment": "mixed",
    "relationIds": ["MTF:weekly_up_monthly_resistance"],
    "evidenceRefs": ["1W:f-2", "1M:f-1"]
  }
}
```

입력 후보는 interval당 최대 6, 심볼 전체 최대 12다. redundancy key로 먼저 dedupe한다.
모든 evidence/fact/condition ID는 interval namespace를 포함한다. 목표 입력은 p95 1,500
tokens 이하이며 실제 usage로 검증한다.
regime, MTF relation, condition, NarrativeFact 생성은 Appendix A §10의 pure rule만 사용한다.

## 5. Strict 출력

```jsonc
{
  "intervalSelections": [
    {
      "interval": "1D",
      "selectedCandidateIds": ["1D:vc-..."],
      "headlineFactIds": ["1D:fact:3", "1D:fact:5"],
      "focusNarratives": [
        {
          "refType": "visualCandidate",
          "refId": "1D:vc-...",
          "factIds": ["1D:fact:11", "1D:fact:12"],
          "watchConditionRef": "1D:cond:1",
          "priority": 1
        }
      ],
      "counterEvidenceRefs": ["1D:event:ev-..."],
      "higherTimeframeRelationIds": ["MTF:weekly_up_monthly_resistance"],
      "emphasisCode": "CONFLICT_FIRST"
    }
  ]
}
```

출력에서 제외하는 필드:

```text
tool, anchorIds, timestamp, price, styleToken, confidence,
keyLevels, invalidation price/text, indicatorSuggestions, 자유 headline/body 문장
```

가격이 필요한 `keyLevelsV2`, confirmation, invalidation, confidence와 모든 사용자-facing
한국어 factual clause는 candidate/fact/condition refs로 서버가 결정론적으로 조립한다.

## 6. Cross-field validator

strict JSON schema 뒤에 의미 validator를 둔다.

- 요청 interval 외 selection 없음
- selected candidate ID가 bundle에 존재
- 같은 ID 중복 없음
- interval/semantic digest 교차 참조 없음
- `visualCandidate` focusNarrative 집합과 selected ID 집합이 정확히 같음
- `ruleFinding` focusNarrative는 bundle ruleFinding allowlist의 부분집합
- fact ID가 해당 ref owner allowlist에 속하고 condition/ref namespace가 일치
- S/T redundancy key와 중복 없음
- interval/전체 visual budget 통과
- schema enum 외 자유 문장이 출력되지 않음
- 빈 I selection도 유효. S/T focus와 정상 빈 레이어 설명은 서버 fallback이 담당

validator 실패는 잘못된 일부 intent를 살리는 대신 해당 symbol LLM 결과 전체를 degraded로
처리한다. S/T와 결정론적 rich commentary는 정상 저장한다.

## 7. 최종 materialize 순서

1. S/T accepted drawings와 deterministic rule focus item 조립
2. selected candidate ID lookup
3. candidate hard gate와 current relevance 재확인
4. S/T와 redundancy/visual budget 최종 확인
5. kernel drawing template 복사 + stable I drawing ID/provenance 설정
6. 검증된 LLM focusNarrative 순서를 적용하되 빠진 S/T drawing은 deterministic focus로 보충
7. accepted drawing 전부의 `focusItems[].drawingIds` coverage invariant 검사
8. fact/condition code-to-text로 한국어 commentary와 compatibility flat 필드 조립

중간 drop이 생기면 LLM commentary를 그대로 쓰지 않는다. deterministic fallback으로 해당
interval commentary를 재조립해 “표시되지 않은 작도 설명”을 원천 차단한다.

## 8. Commentary 순서와 내용

사용자-facing 순서는 LLM 문장 생성이 아니라 검증된 fact의 우선순위 계획에 적용한다.

1. **현재 구조**: regime과 변화 1~2문장
2. **차트에서 볼 것**: 실제 drawing별 `무엇/왜/무엇을 확인`
3. **확인 조건**: 구조가 강화되는 observable condition
4. **무효화 조건**: 해석이 무효가 되는 close/zone condition
5. **상위 주기**: 1M/1W 정합 또는 충돌
6. **반대 근거**: momentum/volume/structure contradiction
7. **데이터 한계**: coverage/estimated VP/order-flow

LLM은 투자 결론이나 문장을 창작하지 않고, 현재 어떤 drawing/fact를 우선 볼지 고른다.
서버가 검증된 한국어 clause로 어떤 관찰에서 해석을 업데이트할지 설명한다.

### drawing이 없는 경우

```text
현재 품질 기준을 통과한 추세선은 표시하지 않았습니다.
가격이 최근 구조 범위의 중앙에 있어 특정 경계를 우선하기 어렵습니다.
다음 확정봉에서 [결정론적 condition]이 발생하는지 확인하세요.
```

“그리지 않음”을 오류나 빈 답변으로 숨기지 않는다.

## 9. Confidence

LLM confidence를 제거하고 selection confidence에 다음 결정론 함수를 쓴다.

```text
base = selected finding quality weighted mean
+ data coverage/freshness
+ independent evidence
+ higher-TF alignment
- counter evidence
- estimated-source dependence
- short history / unresolved role
```

score와 reasons/penalties를 `confidenceV2.selection`에 저장한다. drawing이 0개여도
“빈 레이어 판단의 신뢰도”와 방향 판단을 혼동하지 않는다. 방향 근거가 별도로 성립할
때만 `confidenceV2.marketDirection`을 채우고 아니면 `score=null`이다.

## 10. Responses API 호출 규율

- 기존 urllib/Responses API/strict JSON schema pattern을 유지한다.
- `store:false` 명시
- `max_output_tokens` bounded 설정(초기 hard cap 1,200, p95 목표 650 tokens)
- 응답 `status`, `incomplete_details`, refusal/error, 실제 `model`, `usage` 확인
- 429/5xx만 jitter 없는 bounded backoff 1회; schema/validator defect는 재시도하지 않음
- symbol당 semaphore 1 slot, 전체 동시성은 기존 env로 제한
- raw prompt/response와 API key는 로그·ClickHouse·Redis에 저장하지 않음
- asset에는 exact model, promptVersion, usage totals, latency, selected IDs만 저장

model 선택은 `CHART_ASSET_LLM_MODEL` env를 유지한다. 구현 중 임의로 최신 모델 string으로
바꾸지 않는다. 품질 pilot은 동일 model/snapshot과 promptVersion으로 비교한다.

## 11. 실키 검증

`.env`의 `OPENAI_API_KEY`를 shell output 없이 worker에 주입해 08의 canary를 실행한다.

- mock: schema/refusal/incomplete/timeout/429/5xx/invalid ID
- real: 08 corpus에서 범주별로 고른 최소 12개 real-data symbol episode. episode당 MTF bundle
  1회이며, 품질 신뢰구간이나 reject 원인이 불명확하면 24개까지 확장
- 고정 subset의 curator 평가 harness에서 persistence/no-op을 우회해 동일 bundle을 3회 호출.
  asset은 저장하지 않으며 hard invariant 100%, pairwise selection Jaccard 중앙값 ≥ 0.80
- candidate 선택의 전문가 유용성 blind review
- token/latency/cost 기록
- key/raw response는 artifact에 포함하지 않음

키가 없는 CI에서는 mock+degraded만 통과할 수 있지만, 로컬 구현 완료 판정에는 이번
환경의 real canary가 필수다.

실제 model/prompt 조합이 안정성 또는 전문가 유용성 gate를 통과하지 못하면 rollout에서
Layer I LLM을 비활성화한다. deterministic candidate rank와 rich fallback commentary로
S/T를 계속 제공하며, 불안정한 LLM 선택을 저장하지 않는다.

## 12. Degraded

| 원인 | 결과 |
| --- | --- |
| key 없음/disabled | I 0개 + deterministic commentary |
| timeout/429/5xx | 1회 retry 후 동일 |
| refusal/incomplete/schema | retry 없이 동일 |
| invalid/cross refs | retry 없이 동일 |
| data insufficient | LLM 호출 자체를 하지 않음 |

LLM degraded는 item 저장 성공 + warning이다. 데이터·kernel·storage 실패와 같은 failed
카운터로 세지 않는다(06 상태 계약).

## 13. 용어 사전

rich commentary가 새로 사용하는 다음 개념을 기존 glossary에 보강한다.

```text
접점, 컨센서스, 현재 관련성, 실패한 돌파, 확인 조건,
상위 주기 정합, 반대 근거, 데이터 커버리지
```

초보자용 중립 설명과 영문 alias를 추가하되 짧은 한글 alias 과매칭 규칙을 지킨다.
