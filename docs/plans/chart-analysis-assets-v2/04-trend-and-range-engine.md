# 04. 추세선·채널·Range 엔진

레이어 T는 가장 보수적인 레이어다. 선이 차트를 설명하지 못하거나 현재 의사결정에
영향이 없으면 아무것도 그리지 않는다.

구현 순서, score transform, interval config, tie-break는
`APPENDIX-A-kernel-algorithms.md`가 규범이다.

## 1. 폐기할 v1 불변식

다음은 v2에서 명시적으로 삭제한다.

- `레이어 T 공백 금지`
- `유효 trend가 없으면 최근 40% 고저 Range`
- 두 pivot만 존재하면 trendLine 가능
- touches 동률이면 가파른 slope 우선
- low 양의 slope와 high 음의 slope 절댓값이 비슷하면 channel
- 모든 trend style은 무조건 `extension="ray"`

## 2. 계산 좌표계

후보 fit, current projection, DrawingEntity render가 같은 candle 순서를 사용해야 한다.

- x는 02의 canonical candle sequence/slot이다.
- timestamp는 exact source candle timestamp다.
- `chart-asset:` drawing의 규범 x축은 elapsed milliseconds가 아니라 **candle ordinal**이다.
  현재 renderer의 `pricePerMillisecond` semantic-expansion 경로를 자동 asset에 쓰지 않는다.
  프런트 resolver가 만든 drawingId→resolved slot sidecar로 trend/parallel projection을 계산한다.
  수동 drawing과 일반 proposal의 시간 기반 동작은 그대로다.
- Python quality evaluator와 TypeScript renderer parity fixture를 둔다.
- backend projection과 rendered projection의 asOf 가격 차이가 `0.05 ATR`을 넘으면
  candidate contract failure다.
- large gap/불연속 입력은 좌표 보정으로 숨기지 않고 coverage preflight에서 차단한다.
- offline 기준 범위는 02의 고정 `analysisPriceDomain`이다. 실제 pan/zoom viewport를 hard
  gate나 score에 사용하지 않는다.

## 3. Bounded deterministic consensus

### 입력 축소

display window + 최대 `0.5 * displayBars` anchor context 안의 structural pivot에서:

- high/low 각각 prominence·recency 상위 K=12
- 같은 방향 pivot 사이 최소 separation: `max(3, 0.05 * displayBars)`
- anchor span: 최소 `0.15 * displayBars`, 최대 `1.5 * displayBars`

K 이하에서 모든 유효 pair를 결정론적으로 열거한다. 최대 pair는 side당 66개다. 무제한
lookback O(P²)도 난수 RANSAC도 사용하지 않는다.

### Consensus

각 pair가 만든 line에 대해 같은-kind structural pivot의 local-ATR residual을 구한다.

```text
inlier      abs(pivotPrice - lineAtPivot) <= 0.45 localATR
independent touch
            이전 touch cluster와 minTouchGapBars 이상 분리
robust fit  consensus set의 median pairwise slope + median intercept
```

robust fit은 후보 집합을 안정화하는 reference이지 그대로 렌더하지 않는다. consensus 안의
실제 pivot pair를 다시 열거하고 다음 순서로 최소인 pair 하나를 고른다.

```text
materialized pair loss = p90 local-ATR residual
                       + 0.5 * abs(asOf projection - robust fit projection) / currentATR
                       + 0.1 * median residual
tie-break              = 더 많은 inlier -> 더 긴 span -> 더 최근 last touch -> pivot ID
```

최종 DrawingEntity는 그 실제 pair의 timestamp와 pivot price를 anchor로 쓴다. 이후 touch,
residual, close/wick violation, slope, current distance, current projection을 **materialized
line으로 전부 다시 계산**하고 hard gate를 다시 통과시킨다. 계산된 임의 시간/가격을
anchor로 만들지 않으며, quality를 계산한 선과 renderer가 잇는 선이 달라질 수 없다.

- `slopeAtrPerBar = rawSlope / median(atrAtBar)`이며 ATR median 범위는 first anchor부터
  asOf까지다.
- violation도 first anchor부터 asOf까지의 confirmed candle만 본다.
- residual은 각 touch 시점의 local ATR을 사용한다.

## 4. TrendLine hard gate

support line은 low pivot consensus, resistance line은 high pivot consensus로 만든다.
둘 다 signed slope를 허용해 channel pairing을 가능하게 하지만 standalone 의미는 별도
검증한다.

필수 조건:

1. 독립 touch episode ≥ 3
2. 두 seed anchor 외 consensus confirmation ≥ 1
3. materialized line median residual ≤ 0.35 ATR, p90 residual ≤ 0.75 ATR
4. anchor span ≥ interval별 최소 span
5. slope magnitude ≤ 0.35 ATR/bar
6. support: confirmed close가 line 아래 `0.35 ATR` 초과 침범 0회
7. resistance: confirmed close가 line 위 `0.35 ATR` 초과 침범 0회
8. wick violation은 별도 penalty이며 `0.8 ATR` 초과 반복 시 reject
9. coverage eligible
10. current relevance gate 통과

마지막 1~2봉의 실제 break는 “아직 살아 있는 line” 예외가 아니다. trend candidate를
탈락시키고 03의 break event 후보로 보낸다.

## 5. 현재 관련성 gate

`currentDistanceAtr = abs(currentClose - lineAtAsOf) / currentATR`다.

다음 중 하나를 만족해야 한다.

- `currentDistanceAtr <= 1.5`
- 마지막 confirmed touch가 displayBars의 최근 20% 안이고 `currentDistanceAtr <= 3.0`
- 최대 `0.10 * displayBars` forward horizon 안에 line이 현재 가격 band `±1 ATR`와 교차

그리고 모두 만족해야 한다.

- asOf projection이 `analysisPriceDomain` 안
- 마지막 touch age ≤ `0.35 * displayBars`
- first anchor가 canonical display window 밖이면 display window 안의 confirmed touch가 최소 1개
- required oldest anchor가 frontend bounded history cap 안
- line을 다시 만날 가능성이 없는 방향으로 멀어지는 경우 reject

forward intersection은 현재 close를 고정한 band `[close-1ATR, close+1ATR]`와
`lineAt(asOf)..lineAt(asOf+horizon)` 구간이 겹치는지로 계산한다. `moving_away`는 horizon
끝의 band distance가 현재 distance보다 `0.5 ATR` 이상 커지고 중간 어느 slot에서도
줄지 않는 경우다. 두 계산은 signed slope와 canonical ordinal만 사용한다.

따라서 화면 밖에서 시작한 추세선도 현재 가격과 가까우며 최근 접점이 있으면 살아남는다.
오래됐다는 이유만으로 탈락시키지 않지만, 현재 영향이 없으면 그리지 않는다.

## 6. Trend score

hard gate 통과 후:

```text
0.25 independent touch quality
0.20 current relevance
0.15 last-touch recency
0.15 fit residual/consensus ratio
0.10 span stability
0.10 volume/structure confirmation
0.05 higher-TF alignment
- wick, coverage, counter-trend penalties
```

단일 best trendLine만 layer T 후보가 된다. support와 resistance가 모두 통과해도 서로
channel이 아니면 score/current relevance가 높은 하나를 고른다. 두 선을 채우지 않는다.

## 7. Channel

channel은 lower support line과 upper resistance line의 pair다.

channel boundary는 standalone trend를 화면에 따로 내보내는 후보가 아니라 pair 전용
sub-candidate다. 각 boundary는 TrendLine의 span/slope/residual/violation/coverage 검사를
통과하되, touch 조건만 아래 channel 전용 조건을 쓴다. 따라서 2-touch boundary 하나가
독립 trendLine으로 표시되지는 않는다.

hard gate:

- 두 line의 **signed slope 방향이 같음**
- normalized slope 차이 ≤ 15%
- 시간 overlap ≥ 둘 중 짧은 span의 70%
- 각 경계 touch ≥ 2, 합계 ≥ 5, 적어도 한 경계는 독립 touch ≥ 3
- 동일 x에서 계산한 channel width가 양수
- median width 2~12 ATR, width 변동계수 ≤ 0.25
- candle close containment ≥ 90%
- asOf current가 channel 안 또는 경계 1 ATR 이내
- 어느 경계도 confirmed violation 없음

slope 비교의 공통 ATR은 두 경계 overlap 구간 candle의 median ATR이다. width도 동일한
canonical x에서 이 ATR로 정규화한다.

독립 fit 두 개는 channel hypothesis일 뿐이다. 품질이 높은 boundary의 materialized pair를
base anchor 두 개로 쓰고, 반대 경계의 실제 pivot 중 base line 동일 x projection과의 offset이
reference median width에 가장 가까운 점을 세 번째 `trendParallelLines` anchor로 쓴다.
그 세 anchor로 renderer와 동일한 완전 평행 두 경계를 다시 구성한 뒤 양쪽 residual/touch,
close·wick violation, containment, width/CV, slope, current relevance를 전부 재계산한다. 최종
render geometry가 hard gate를 다시 통과하지 못하면 channel을 버린다.

수렴/확산 slope는 channel로 부르지 않는다. 향후 wedge/triangle 후보가 필요하면 별도
semantic type과 두 trendLine template로 평가하며, v2 첫 구현에서 이름만 붙이지 않는다.

## 8. Range는 독립 finding이다

Range 후보 window는 최근 `0.3~0.8 * displayBars`의 bounded set을 비교한다.

경계:

- raw max/min이 아니라 robust 5/95 percentile + structural pivot zone
- upper/lower 각각 독립 touch ≥ 2
- touch가 시간상 교대해 range 왕복을 확인
- boundary slope `abs(slope) <= 0.05 ATR/bar`
- close containment ≥ 85%
- width 2~10 ATR
- 현재가가 box 안 또는 경계 0.5 ATR 이내
- active confirmed breakout/breakdown 없음
- 최근 전체 window를 한 번의 큰 trend swing으로 더 잘 설명하면 reject

여러 window가 통과하면 out-of-sample 마지막 20% containment/reaction이 좋은 하나를
고른다. 그 결과가 없으면 `trends=[]`, layer T `drawings=[]`,
`emptyReason="no_valid_trend_or_range"`다.

도구는 `rangeBox` 하나이며 start/end timestamp는 실제 source candle이다. 과거에 끝난
range는 현재 breakout/retest event를 설명하는 짧은 segment 후보로 Layer I에 넘길 수
있지만 active Layer T Range로 그리지 않는다.

## 9. Extension 정책

| 상태 | extension |
| --- | --- |
| active current-relevant trend | `ray` |
| 이미 break되어 event 문맥만 남은 역사 구조 | `segment` (Layer I 후보) |
| channel | renderer의 bounded parallel line, active span/current projection만 |
| uncertain/two-point tentative | drawing 없음 |

ray는 현재 relevance를 통과한 후보에만 허용한다. future projection은 최대 bounded
horizon metadata를 가지며 투자 예측으로 표현하지 않는다.

## 10. Fibonacci 후보

Fibonacci는 Layer T 기본 출력이 아니라 Layer I 후보다.

필수 조건:

- alternating structural pivot의 명확한 impulse
- impulse ≥ 4 ATR, prominence/volume 참여 확인
- 이후 retracement가 23.6~78.6% 안
- impulse 이후 structure invalidation 없음
- current relevance와 higher-TF 맥락 있음
- 다른 range/level drawing보다 추가 정보가 있음

LLM은 pivot pair를 선택하지 않는다. kernel이 완성한 Fib candidate ID만 선택할 수 있다.
정확한 impulse/current relevance/redundancy/anchor gate는 Appendix A §9.3을 따른다.

## 11. NVDA 고정 회귀

기존 저장 선:

```text
2025-09-17 168.41 -> 2025-09-25 173.12, touches=2, ray
```

v2에서는 `input_contract_mismatch`만으로 통과 처리하지 않는다. 최소한 아래 구조 품질
reason 중 하나 이상으로 반드시 탈락해야 한다.

```text
two_point_only
no_independent_confirmation
stale_last_touch
outside_current_relevance
```

`input_contract_mismatch`는 추가 reason으로 기록할 수 있지만 위 네 품질 reason을 대신하지
못한다.

단, 향후 canonical 데이터가 보강되어 같은 두 anchor 주변에 실제 세 번째 독립 touch와
현재 relevance가 생기면 새로운 digest/version에서 다시 평가할 수 있다. 심볼명으로
특별 차단하지 않는다.

## 12. 테스트

- two-point line reject와 third-touch confirmation
- adjacent candles가 touches를 부풀리지 않음
- wick touch vs close violation
- local ATR residual과 volatility regime 변화
- stale/offscreen/current-distance/forward-intersection gate
- backend projection과 frontend render parity
- rising/descending parallel channel
- converging wedge가 channel이 아님
- channel width를 동일 x에서 계산
- true range vs trending window
- active breakout이면 Range reject
- no candidate → empty layer success
- NVDA/AAPL 실제 snapshot 회귀
- bounded candidate count와 실행 시간
