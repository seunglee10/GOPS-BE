# Appendix A. Kernel v2 규범 알고리즘과 초기 설정

상태: README와 같은 검토 단계
적용 버전: `qualityPolicyVersion=chart-quality-v1`, `kernelVersion=kernel-v2`

이 문서는 03·04의 구현자 재량을 줄이는 규범 부록이다. 아래 값은 08의 development/tuning
episode에서만 조정할 수 있다. 조정하면 한 곳의 versioned config, exact reject-reason golden,
평가 결과, `qualityPolicyVersion`을 같은 커밋에서 갱신한다. 코드 곳곳에 별도 magic number를
둘 수 없다.

## 1. 공통 규칙

```text
clip01(x)       = min(1, max(0, x))
atrNorm(d, i)   = abs(d) / max(atrAtBar[i], epsilon)
recency(age, h) = exp(-ln(2) * age / h)
```

- 모든 percentile은 정렬된 값의 선형 보간으로 계산한다.
- median/MAD/weighted median의 짝수·동률 규칙을 pure helper 하나로 고정한다.
- OHLCV와 `atrAtBar`는 asOf 이하만 본다. pivot은 `confirmedAt <= asOf`만 쓴다.
- 정렬 tie-break는 별도 표기가 없으면 `final quality score desc → current distance asc → last evidence
  bar desc → span desc → stable ID asc`다.
- 모든 evidence ID는 `{interval}:{type}:{localId}` namespace를 가진다.
- float는 계산 중 반올림하지 않는다. schema materialize에서만 기존 price precision을 쓴다.
- NaN/Infinity/0 이하 ATR은 해당 capability contract error다. 임의 epsilon으로 drawing을
  살리지 않는다.

## 2. Interval config

| 값 | 1D | 1W | 1M |
| --- | ---: | ---: | ---: |
| displayBars | 120 | 104 | 36 |
| 최대 extra anchor context | 60 | 52 | 18 |
| pivotConfirmBars 최소 | 3 | 2 | 1 |
| same-kind pivot separation | 5 | 3 | 2 |
| minTouchGapBars | 5 | 3 | 2 |
| reaction horizon bars | 10 | 6 | 3 |
| level recency half-life | 24 | 16 | 8 |
| level last-touch max age | 42 | 36 | 14 |
| event relevance bars | 30 | 16 | 12 |
| extreme episode gap bars | 10 | 4 | 2 |
| volume baseline bars | 20 | 13 | 12 |
| retest follow-through bars | 3 | 2 | 1 |
| forward trend horizon | 12 | 8 | 4 |

lookback은 기존 `LOOKBACK_BARS`를 유지한다. display/extra context를 넘어선 candle은 52주
extreme, higher-TF summary, long-history prominence baseline에만 쓰고 화면 drawing anchor를
무제한 과거로 확장하지 않는다.

## 3. ATR

Wilder ATR(14)을 순방향으로 계산한다.

1. true range는 `max(high-low, abs(high-prevClose), abs(low-prevClose))`다.
2. 첫 14개 유효 TR 전에는 현재까지 TR median을 `atrAtBar`로 쓴다.
3. 14개부터 첫 seed는 14개 mean, 이후 `((prevATR*13)+TR)/14`다.
4. split/correction으로 한 bar TR이 직전 ATR의 12배를 넘으면 계산은 유지하되
   `abnormal_true_range` quality flag를 남긴다. 이 flag가 최근 contiguous window에 있으면
   trend/level capability를 fail-closed한다.

## 4. Pivot

### 4.1 Directional-change 확인

tactical과 structural detector를 같은 알고리즘, 다른 threshold로 각각 실행한다.

```text
tactical threshold(i)   = max(1.25 * atrAtBar[i], 0.010 * candidatePrice)
structural threshold(i) = max(2.00 * atrAtBar[i], 0.015 * candidatePrice)
```

1. 방향 미정 상태에서 running high/low와 그 source bar를 갱신한다.
2. running high 이후 candle low가 `runningHigh-threshold(highBar)` 이하이고 최소
   `pivotConfirmBars`가 지났으면 high pivot을 확정한다. `timestamp`는 high bar,
   `confirmedAt`은 reversal bar다.
3. low는 대칭 규칙을 쓴다.
4. 확정 후 반대 extreme부터 상태를 전환한다. 마지막 미확정 extreme은 버린다.
5. same-kind pivot separation 안의 후보는 prominence, reversal ATR, 최근 bar, stable ID 순으로
   강한 하나만 남긴다.

high의 `reversalAtr=(runningHigh-confirmationBar.low)/atrAtBar[high]`, low는
`(confirmationBar.high-runningLow)/atrAtBar[low]`다.

### 4.2 Prominence

확정된 high pivot `p`:

```text
leftBase  = min(low) from previous confirmed low through p
rightBase = min(low) from p+1 through confirmedAt
prominence = p.high - max(leftBase, rightBase)
```

low는 max(high)와 부호를 반대로 쓴다. 양쪽 basin이 없으면 structural로 승격하지 않는다.
`prominenceAtr = prominence / atrAtBar[p]`다. structural은 기존 reversal threshold 외에
`prominenceAtr >= 2.0`을 요구한다.

```text
strength = 0.45 * clip01(prominenceAtr / 4)
         + 0.35 * clip01(reversalAtr / 4)
         + 0.20 * recency(ageBars, 0.5 * displayBars)
```

## 5. Level zone

### 5.1 Seeded bounded clustering

single-link chain을 금지한다.

1. structural pivots를 `strength desc → barIndex desc → ID asc`로 정렬한다.
2. 아직 미할당인 pivot을 seed로 삼는다.
3. seed와 가격 거리가 `0.60 * median(seedATR, pivotATR)` 이내인 미할당 pivot만 임시 집합에
   넣는다.
4. 임시 집합 전체 `max(price)-min(price)`가 집합 median ATR의 `0.80`을 넘으면 먼 pivot부터
   제거한다. 동률이면 약한/오래된 pivot을 먼저 제거한다.
5. 다른 seed cluster와 겹치더라도 자동 merge하지 않는다. combined width ≤0.80 ATR이고
   weighted center 차이 ≤0.35 ATR일 때만 합친 뒤 폭 조건을 다시 검사한다. 아니면 후속
   redundancy rank에서 하나를 고른다.

weight는 `0.25 + 0.75*strength`다. center는 weighted median이다.

```text
madAtr       = weightedMedian(abs(price-center)) / medianATR
halfWidthAtr = clamp(1.4826 * madAtr, 0.075, 0.40)
zoneLow/High = center ± halfWidthAtr * medianATR
```

전체 폭은 0.15~0.80 ATR이다.

### 5.2 Touch episode

candle wick `[low, high]`가 zone과 겹치면 진입이다. 이미 episode 안이면 연속 겹침은 새
touch가 아니다.

- close가 zone에서 정상 역할 방향(support는 위, resistance는 아래)으로 `0.75 localATR`
  이상 이탈하면 성공 episode를 끝낸다.
- zone을 관통해 반대쪽 `0.35 ATR` 밖에서 close하면 failed episode로 끝낸다.
- 이전 episode 종료 뒤 `minTouchGapBars`가 지나야 다음 독립 touch로 센다.
- reaction MFE/MAE는 episode 종료 다음 bar부터 `min(next episode start, reaction horizon,
  asOf)`까지 역할 방향으로 계산한다. horizon이 2 bars 미만이면 reaction-confirmed로 세지
  않지만 touch evidence는 유지한다.

support MFE는 zone high에서 이후 최고 high까지, resistance는 zone low에서 이후 최저
low까지의 유리한 거리다. MAE는 반대 방향이다.

### 5.3 Level score term

hard gate 통과 뒤 각 term은 다음과 같다.

```text
touchQuality = 0.5*clip01((episodeCount-1)/3)
             + 0.5*median(clip01(directionMfeAtr/2))
recency      = recency(lastTouchAge, configured half-life)
reaction     = median(clip01((directionMfeAtr-maeAtr)/2))
relevance    = 1-clip01(currentDistanceAtr/4)
roleFlip     = 1 if confirmed role_flip else 0
vpConfirm    = 1 confirmed, 0.5 estimated confluence, 0 absent
otherConfirm = clip01(valid confirmation count/2)
```

03의 weight를 곱한다. 음수 reaction은 0으로 clip한다. 최종 동률은 공통 tie-break다.

### 5.4 역할 상태와 구조 선택

접근 방향은 episode 시작 직전 close가 zone 위/아래인지로 정한다.

| 현재 내부 상태 | 관찰 | 다음 상태 / public role |
| --- | --- | --- |
| unresolved | 위에서 touch, 위쪽 reaction ≥1 ATR | support_active / support |
| unresolved | 아래에서 touch, 아래쪽 reaction ≥1 ATR | resistance_active / resistance |
| support_active | close < zoneLow-0.25 ATR | break_down_pending / drawing suspend |
| resistance_active | close > zoneHigh+0.25 ATR | break_up_pending / drawing suspend |
| break_up_pending | 위에서 retest hold + follow-through | role_flip_support / role_flip |
| break_down_pending | 아래에서 retest hold + follow-through | role_flip_resistance / role_flip |
| break_*_pending | follow-through 전에 zone 반대편 복귀 | unresolved; failed-break event 생성 |
| break_*_pending | 같은 break side 두 번째 종가 또는 participationPass+다음 hold | invalidated; 기존 role drawing 제거 |
| invalidated | 새 독립 touch + 방향 reaction ≥1 ATR | 새 episode로 support/resistance 재활성화 |

pending 상태는 강한 level label로 표시하지 않는다. role flip은 반드시 break event와 retest
event ID를 둘 다 참조한다. 과거 touch 수만으로 invalidated zone을 재활성화하지 않는다.
이후 break 검사에서 `role_flip_support/resistance`는 각각 support_active/resistance_active와
같이 취급한다.

구조 layer Pareto 선택의 distance band는 `near<=1.5 ATR`, `actionable<=3 ATR`,
`context<=4 ATR`, 그 밖은 탈락이다. 후보 A가 B보다 distance band, final quality score,
last-touch recency에서 모두 나쁘지 않고 하나 이상 엄격히 좋으면 B를 지배한다. nondominated
후보를 `band → score → recency → ID`로 정렬해 local support/resistance 각각 최대 1개를
고른다. macro 후보는 선택된 local과 0.5 ATR 이상 떨어지고 redundancyKey가 다를 때만 최대
1개 추가한다. higher-TF라는 이유만으로 local을 대체하지 않는다.

## 6. Event

### 6.1 Break와 participation

signal close가 zone boundary에서 `0.25 localATR` 밖이고 이전 close가 zone/반대편이면 break
state를 시작한다. volume baseline은 signal을 제외한 직전 config bars의 median이다.

```text
relativeVolume = signalVolume / max(baselineMedian, 1)
participationPass = relativeVolume >= 1.20
                 or signal dollar volume percentile in baseline >= 0.65
```

participation이 없으면 event 상태는 남기되 Flag priority를 한 band 낮춘다. 다음 close가
boundary 안으로 되돌아오면 `failed`; 밖에서 유지하면 `hold`; zone 재접촉 후 config의
follow-through 안에 다시 바깥 종가를 만들면 `retest_confirmed`다.

### 6.2 Gap·extreme·중복

- gap은 `abs(open-prevClose) >= 1.0 localATR`다. 이후 wick이 gap interval 전체를 덮은 최초
  bar에서 filled가 된다.
- 연속 52주 extreme은 같은 방향이고 config의 `extremeEpisodeGapBars` 안에 이어지는 동안
  하나의 episode다. 중간 close가 직전 extreme에서 1 ATR 이상 반대로 이탈하면 episode를
  종료한다. 대표 anchor는 최신 extreme이다.
- dedupe key는 `eventType + referencedZoneId + stateEpisodeStart`다.
- 선택 tie-break는 `currentImpact band → state(unresolved/retest/failed) → age asc → evidence
  strength desc → ID asc`다.

`currentImpact`는 high/medium/low다. high는 active retest·failed break·unfilled gap이 현재
1.5 ATR 안이거나 상태 전이가 최근 `0.10*displayBars` 안인 경우, medium은 unresolved이며
3 ATR/기본 relevance window 안인 경우다. 나머지는 low이고 visual 후보가 될 수 없다.

```text
eventEvidenceStrength = 0.35*referencedStructureQuality
                      + 0.25*participationQuality
                      + 0.20*followThroughQuality
                      + 0.20*recency(age, eventRelevanceBars)
```

각 quality term은 0~1이다. participation이 없으면 0, follow-through 미확정이면 0이다.

## 7. Trend와 channel

04의 K=12 pair enumeration을 따른다. 각 hypothesis의 inlier를 local ATR residual 0.45로
구하고 독립 touch cluster를 `minTouchGapBars`로 묶는다.

1. inlier ≥3인 hypothesis만 robust reference fit을 만든다.
2. consensus pivot pair를 다시 열거해 04의 materialized pair loss를 최소화한다.
3. materialized line에서 모든 metric과 hard gate를 다시 계산한다.
4. `slopeAtrPerBar`의 denominator는 first anchor~asOf ATR median이다.
5. support/resistance violation은 first anchor~asOf confirmed close에만 적용한다.

score term:

```text
touchQuality = 0.5*clip01((touchCount-2)/3) + 0.5*medianReactionQuality
relevance    = 1-clip01(currentDistanceAtr/3)
lastTouch    = recency(lastTouchAge, 0.20*displayBars)
fit          = 1-clip01(medianResidualAtr/0.35)
consensus    = clip01(inlierCount/max(candidatePivotCount, 3))
span         = clip01(spanBars/(0.60*displayBars))
```

`medianReactionQuality`는 inlier touch 각각에 5.2의 horizon을 적용한
`clip01((directionMfeAtr-maeAtr)/2)`의 median이다. 관찰 horizon이 2 bars 미만인 touch는
이 median에서 제외하고 touch count에만 포함한다.

`fit/consensus`는 04의 0.15 weight 안에서 반씩 쓴다. volume/structure와 MTF는 실제 ref가
있을 때만 1, 없으면 0이다.

channel pair는 각 boundary가 residual/violation/span/slope gate를 통과해야 한다. touch만
`2+3 이상, 합계 5 이상`을 허용한다. 공통 ATR은 overlap 구간 median이다. 같은 x의 width
series로 median/CV/양수를 계산한다.

독립 boundary는 hypothesis다. 최종 base materialized pair와 반대 실제 pivot으로
`trendParallelLines` 세 anchor를 만든 뒤 renderer와 같은 ordinal parallel geometry를
재구성한다. residual, touch, violation, containment, width/CV, current relevance를 그 최종
두 선으로 다시 계산하며 하나라도 channel gate를 벗어나면 reject한다.

## 8. Range

각 interval displayBars의 `0.30, 0.40, 0.50, 0.60, 0.70, 0.80` 배를 floor한 여섯 window만
검사한다. 모두 asOf에서 끝난다.

1. 앞 80% fit 구간에서 lower는 lows의 p05, upper는 highs의 p95로 시작한다.
2. 해당 값 0.45 ATR 안의 structural pivot weighted median으로 경계를 보정한다. pivot이
   없으면 percentile을 유지하지만 quality penalty를 준다.
3. fit과 마지막 20% validation 모두에서 각 경계 touch가 최소 1개이고, 전체 각 경계 2개
   이상이어야 한다.
4. touch representative가 lower/upper를 최소 세 번 교대해야 한다.
5. validation containment/reaction, 전체 containment, width, current relevance hard gate를
   04대로 적용한다.

lower/upper boundary slope는 해당 경계 touch representative의 모든 pair slope median
(Theil-Sen)으로 계산하고 window median ATR로 정규화한다. 한 경계 touch가 2개 미만이면
slope를 0으로 대신하지 않고 실패다.

trend dominance reject:

```text
directionalEfficiency = abs(lastClose-firstClose) / sum(abs(close[i]-close[i-1]))
netTravelRatio        = abs(lastClose-firstClose) / rangeWidth
orderedSwingRatio     = monotonic-direction structural swings / all structural swings
reject range if directionalEfficiency >= 0.45
             and netTravelRatio >= 0.70
             and orderedSwingRatio >= 0.70
```

여러 window는 `validation containment → validation reaction → boundary touch count → 짧은
window → start candleKey` 순으로 선택한다. validation을 fit에 다시 섞어 경계를 조정하지
않는다.

## 9. Layer I candidate generator

정의되지 않은 semantic type은 palette에 넣지 않는다. 모든 I 후보는 다음 공통 gate를
통과한다.

- source S/T/event 후보의 hard gate 통과, exact canonical anchor membership
- final quality score ≥0.65. event 계열은 currentImpact medium 이상
- confirmation/invalidation condition ref 존재
- 선택된 S/T와 같은 redundancyKey가 아니고 geometry 추가 정보가 있음
- 현재 close 또는 active event에서 3 ATR 이내. 아래 type별 더 엄격한 값이 우선
- final template을 만든 뒤 tool contract와 visual budget 재검사

### 9.1 넓은 price zone

- 5절 level hard pass, zone width ≥0.25 ATR, current distance ≤2 ATR
- selected S H-Line/zone과 같은 center가 0.5 ATR 안이면 reject
- higher-TF relation 또는 VP/order-flow confirmation 중 하나가 있어야 함
- invalidated/unresolved role 금지

### 9.2 Consolidation base

- 8절 range hard pass 기록이 있고 upper/lower 양쪽 validation touch가 있음
- active T range와 같은 box면 reject
- 최근 confirmed breakout/retest가 config event relevance 안이고 현재가가 box boundary
  2 ATR 안일 때만 historical `rangeBox` segment 후보
- volume contraction 뒤 break participation이 확인된 경우에만 사용자 문구에서
  `수급을 동반한 베이스`를 허용한다. 그 외에는 `횡보 베이스/통합 구간`으로만 표현
- 과거 box 종료 anchor는 break source candle이며 현재까지 무제한 확장하지 않음

### 9.3 Fibonacci retracement

alternating structural pivot A→B에 대해:

```text
spanBars in [0.05, 0.60] * displayBars
impulseDistance >= 4 * medianATR(A..B)
directionalEfficiency(A..B) >= 0.55
internal opposite structural retracement < 0.382 * impulseDistance
```

- B 이후 current retracement가 23.6~78.6% 안이고 현재가가 38.2/50/61.8 중 하나에서 1 ATR
  이내여야 함
- origin A 반대편 confirmed close가 있으면 invalidated
- impulse participation 또는 eligible higher-TF alignment 중 하나 필요
- fib 주요 선이 selected S level과 모두 0.5 ATR 안이거나 selected range 안에 완전히
  포함되면 추가 정보가 없으므로 reject
- anchor는 실제 A/B pivot timestamp/price이고 LLM이 pair를 고르지 않음

### 9.4 Event window

- 같은 zone/structure를 참조하는 두 confirmed state event가 있어야 함
- 허용 pair는 `break→retest`, `gap→fill/failed-fill`, `range-break→failed-break`뿐
- span은 3 bars 이상, 0.35*displayBars 이하
- 두 번째 event가 currentImpact high이고 config event relevance 안
- `verticalParallelLines` 두 anchor는 두 event source candle timestamp이며 y를 만들지 않음
- 단순히 이벤트가 두 개 있다는 이유로 시간 구간을 만들지 않음

### 9.5 중요한 단일 event

- 6절 event hard pass, currentImpact high, eventEvidenceStrength ≥0.65
- S layer에서 같은 event/redundancyKey Flag가 이미 선택됐으면 reject
- failed/filled/resolved 뒤 현재 영향이 사라진 event는 reject
- anchor는 02의 event별 x/y 규범을 그대로 사용

### 9.6 Historical broken structure

- break 전 시점에서 trend/range가 해당 hard gate를 실제로 통과했던 evidence snapshot 필요
- confirmed break가 config event relevance 안이고 현재 retest/failed-break가 원 구조를 참조
- 현재가가 broken line/box projection 또는 boundary 1.5 ATR 안
- active T와 같은 geometry면 reject
- segment는 original first anchor부터 break event candle까지만이며 ray 금지

## 10. Regime · condition · narrative fact · indicator

### 10.1 Regime과 MTF relation

최근 3개 structural high/low가 모두 higher면 swing trend `up`, 모두 lower면 `down`, 아니면
`mixed`다. 보조 close trend는 최근 `min(30, 0.25*displayBars)` closes의 Theil-Sen slope를
같은 구간 median ATR로 정규화한다.

```text
close up   = slopeAtrPerBar >= 0.05 and directionalEfficiency >= 0.35
close down = slopeAtrPerBar <= -0.05 and directionalEfficiency >= 0.35
sideways   = abs(slopeAtrPerBar) < 0.05 or efficiency < 0.35
```

swing과 close trend가 같은 방향이면 up/down, 하나만 방향성이면 해당 방향 `weak`, 서로
반대면 mixed다. volatility는 currentATR / 최근 min(60, displayBars) ATR median이 `<0.8 low`,
`0.8~1.25 normal`, `>1.25 high`다. momentum slowing은 같은 부호인 최근 10-bar slope의
절댓값이 직전 10-bar보다 40% 이상 감소한 경우다. sign change는 3 bars 안의 structural
break가 있을 때만 momentum transition fact가 된다.

MTF relation enum:

- `aligned_up|aligned_down`: eligible 주기들의 방향 regime이 모두 같음
- `higher_tf_resistance|higher_tf_support`: 상위 주기 accepted zone이 현재가 2 ATR 안
- `mixed`: 방향이 충돌하거나 상위 zone과 하위 추세가 반대
- `insufficient`: eligible higher-TF 없음

오래된 prose를 relation 근거로 쓰지 않고 ruleDigest/evidence ID만 쓴다.

### 10.2 Condition과 NarrativeFact

```text
Condition {
  conditionId, kind, subjectRef, thresholdSourceRef,
  confirmationBars, evidenceRefs[], renderCode
}
NarrativeFact {
  factId, ownerRef, clauseCode, parameterRefs[], evidenceRefs[]
}
```

허용 condition kind는 `close_above_zone`, `close_below_zone`, `hold_zone`, `retest_hold`,
`trend_close_violation`, `range_exit`, `event_resolved`, `fib_origin_invalidated`다. threshold와
가격은 referenced geometry에서 서버가 읽고 LLM output에 넣지 않는다.

| semantic | confirmation | invalidation |
| --- | --- | --- |
| support/resistance | 역할 방향 close hold 또는 새 reaction | 반대 경계 0.25 ATR 밖 confirmed close |
| trend | 새 touch 뒤 역할 방향 hold | 04 close violation |
| channel | 현재 경계 reaction/containment 유지 | 어느 경계든 confirmed violation |
| range | 경계 reaction과 containment | boundary 0.25 ATR 밖 confirmed break |
| fib | relevant fib band hold/reaction | origin 반대편 confirmed close |
| event | state별 hold/retest follow-through | failed/resolved state transition |

허용 fact code는 versioned enum으로 관리한다. 초기 필수 set은
`ACTIVE_SUPPORT_NEAR`, `ACTIVE_RESISTANCE_NEAR`, `TREND_RECENTLY_CONFIRMED`,
`CHANNEL_CONTAINED`, `RANGE_ACTIVE`, `BREAK_RETEST_ACTIVE`, `FAILED_BREAK_ACTIVE`,
`FIB_RETRACEMENT_RELEVANT`, `NO_VALID_STRUCTURE`, `NO_VALID_TREND`,
`HIGHER_TF_ALIGNMENT`, `HIGHER_TF_CONFLICT`, `DATA_CAVEAT`, `INDICATOR_REASON`이다.
code-to-text는 evidence/condition 값을 넣어 한국어 clause를 만들고 glossary alias를 함께
제공한다. enum 밖 fact/condition, owner/evidence가 없는 fact는 생성·LLM 입력·표시를 금지한다.

### 10.3 자동 indicator gate

- Bollinger(20, 2): bandwidth가 가용 최근 최대 120봉의 20th percentile 이하이고, 8절
  range width/ATR도 최근 20th percentile 이하인 confirmed compression
- MACD(12,26,9): histogram zero-cross가 최근 3 bars 안이고 같은 방향 structural break가
  같은 3 bars 안에 있음
- RSI Wilder(14): RSI ≤30 또는 ≥70 뒤 3 bars 안에 threshold 안으로 복귀하고 같은 bar
  window에 accepted level reaction이 있음

각 finding은 evidenceStrength를 `structure 0.5 + recency 0.3 + participation 0.2`로 계산한다.
hard pass 중 가장 높은 하나만 추천하고 동률은 MACD → Bollinger → RSI다. 해당 focus fact와
what-to-watch condition을 만들 수 없으면 지표를 켜지 않는다.

## 11. Reason code와 golden

최소 표준 reason:

```text
data: insufficient_coverage, stale_input, noncanonical_anchor, abnormal_true_range
pivot: unconfirmed, low_prominence, same_kind_too_close
level: one_touch_only, weak_reaction, stale_last_touch, too_far, invalidated, unresolved_role
trend: two_point_only, no_independent_confirmation, high_residual, close_violation,
       stale_last_touch, outside_current_relevance, moving_away
channel: slope_direction_mismatch, nonparallel, insufficient_boundary_touches,
         unstable_width, containment_failed
range: insufficient_boundary_touches, no_alternation, active_break, trend_dominant,
       validation_failed
event: duplicate_episode, stale_event, filled_gap, failed_without_current_impact
agent: unknown_candidate, invalid_fact_ref, redundant_with_rule, unstable_selection
insight: weak_additional_value, low_current_impact, duplicate_rule_geometry,
         invalid_impulse, stale_broken_structure, unsupported_semantic
narrative: unknown_fact_code, missing_condition, evidence_owner_mismatch
```

golden fixture는 최종 drawing만 비교하지 않는다. accepted/rejected candidate ID, primary
reason, materialized anchor, evidence refs, score term을 exact 비교한다. score float는
`1e-6` tolerance를 사용하고 tie-break 결과는 exact다.
