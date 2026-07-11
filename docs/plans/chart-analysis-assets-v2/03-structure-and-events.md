# 03. 구조·레벨·이벤트 엔진

위치: `systems/market-data/shared/alfaka/analytics/`의 pure kernel과
`gops_agents/chart_assets/compilers.py`. 수치 계산은 market-data, 의미 선택과
DrawingEntity materialize는 agent-orchestration 소유를 유지한다.

수식, interval별 초기값, no-lookahead, tie-break, reason code는
`APPENDIX-A-kernel-algorithms.md`가 규범이다. 이 문서의 개념 설명과 부록이 충돌하면
더 구체적인 부록을 따른다.

## 1. 모듈 경계

```text
analytics/
  analysis_candles.py  # 02
  atr.py               # 시점별 Wilder ATR
  pivots.py            # tactical/structural confirmed pivot + prominence
  levels.py            # zone, touch episode, reaction, role state
  events.py            # stateful breakout/retest/gap/high-low/volume event
  trends.py            # 04
  candidates.py        # VisualCandidate 조립과 hard gate
  quality.py           # versioned threshold/config와 공통 score
  schema.py            # compact public evidence 조립
```

모든 함수는 입력·config만으로 결정되고 시계·난수·네트워크를 읽지 않는다.

## 2. ATR과 volatility normalization

v1은 최신 ATR 하나로 과거 전체를 평가한다. v2는 각 candle의 `atrAtBar`를 사용한다.

- Wilder ATR(14), no-lookahead
- seed 이전은 available true range의 robust median fallback
- 모든 거리·reaction·residual은 해당 시점 ATR로 정규화
- split/correction 뒤 비정상 ATR jump는 coverage/quality flag로 드러냄
- interval 사이 ATR 절댓값을 직접 비교하지 않음

## 3. Pivot v2

### 두 등급

| 등급 | 용도 | 초기 reversal/prominence |
| --- | --- | --- |
| tactical | 최근 event/range/retest | `max(1.25 ATR, 1.0% price)` |
| structural | level/trend/channel/Fib | `max(2.0 ATR, 1.5% price)` + prominence gate |

기존 zigzag confirmation lag를 유지하되 pivot마다 다음을 기록한다.

```jsonc
{
  "id": "p17",
  "timestamp": "source candle timestamp",
  "candleKey": "...",
  "barIndex": 117,
  "kind": "H",
  "grade": "structural",
  "price": 200.06,
  "confirmedAt": "...",
  "localAtr": 6.12,
  "prominenceAtr": 3.4,
  "separationBars": 18,
  "strength": 0.88
}
```

prominence는 주변 반대 swing baseline과의 수직 거리를 local ATR로 나눈 bounded pure
Python 계산이다. top/bottom 후보 수를 제한하고 같은 방향의 너무 가까운 pivot은 더
강한 것만 남긴다. 마지막 미확정 극점은 drawing anchor가 될 수 없다.

## 4. Level은 점이 아니라 zone으로 계산한다

### 클러스터

- structural pivot을 가격순 single-link로 무조건 묶지 않는다.
- local ATR-normalized 거리와 시간 분리를 함께 사용한다.
- center는 strength/prominence 가중 median, 폭은 weighted MAD와 pivot wick 범위로 계산한다.
- 폭은 `[0.15 ATR, 0.8 ATR]`로 제한한다. 그보다 넓으면 하나의 level이 아니라 후보 분할
  또는 reject다.
- output은 `center`, `low`, `high`, `dispersionAtr`를 가진다.

### 독립 touch episode

한 zone 안의 연속 여러 봉을 한 번의 touch로 센다.

```text
episode start: wick/close가 zone에 진입
episode end:   zone에서 0.75 ATR 이상 이탈하거나 반대편으로 실패
distinct:      이전 episode 종료 후 minTouchGapBars 이상
```

touch마다 접근 side, wick penetration, close hold, 이후 MFE/MAE, 실패 시점, available
volume을 기록한다. reaction은 `abs(price move)`가 아니라 zone 역할 방향의 MFE와 실패
전 유지 시간으로 계산한다.

### 역할 상태

```text
support | resistance | role_flip | unresolved
```

- 현재 close만으로 역할을 정하지 않고 마지막 confirmed approach/hold/break state를 쓴다.
- role flip은 zone 반대편 종가 확정 후 재접촉·방어가 있어야 한다.
- zone 안에 현재가가 있으면 `unresolved`; 강한 지지/저항으로 단정하지 않는다.
- 정확한 pending/invalidation/reactivation 전이는 Appendix A §5.4를 따른다.

## 5. Level hard gate와 score

### 기본 hard gate

- 독립 touch episode ≥ 2
- 최소 1개 episode의 방향성 reaction ≥ 1 ATR
- 마지막 유효 touch가 interval별 relevance window 안
- 현재 역할이 `support|resistance|role_flip`
- current distance ≤ 4 ATR 또는 최근 breakout/retest가 직접 참조
- 02의 고정 `analysisPriceDomain` 밖이면 탈락. 사용자 viewport는 사용하지 않음
- 이후 confirmed invalidation이 해결되지 않았으면 탈락

### 명시적 1-touch 예외

다음 모두를 만족하는 최근 structural extreme만 허용한다.

- 52주/전체 lookback high 또는 low
- prominence ≥ 3 ATR
- 현재 거리 ≤ 2 ATR
- 아직 반대편 종가 invalidation 없음
- higher-TF extreme 또는 VP/order-flow 중 하나가 확인

예외 후보는 `fresh_extreme`로 표시하고 일반 반복 레벨보다 낮은 visual priority를 갖는다.

### score

hard pass 후에만 계산한다.

```text
0.25 touch episode quality
0.20 recency decay
0.20 directional reaction / hold quality
0.15 current relevance
0.10 role-flip confirmation
0.05 volume-profile confirmation
0.05 volume/order-flow/round-number confirmations cap
- coverage and contradiction penalties
```

VP는 candle-range 추정치라는 quality provenance를 유지한다. `roundNumber`나 VP 하나로
hard gate를 통과시킬 수 없다.

## 6. 구조 layer materialize

### 선택

1. hard-pass local support 중 현재에 가장 유용한 1개
2. hard-pass local resistance 중 현재에 가장 유용한 1개
3. local과 중복되지 않는 higher-TF macro 1개

한쪽 후보가 없다고 반대쪽 후보로 슬롯을 채우지 않는다. current distance, score,
last touch, role을 함께 정렬하고 단순 score만으로 먼 level이 가까운 level을 밀어내지
않게 Appendix A §5.4의 Pareto rank를 쓴다.

### 도구 선택

| geometry | 도구 |
| --- | --- |
| 폭이 0.25 ATR 미만인 명확한 level | `horizontalLine` |
| 의미 있는 폭을 가진 support/resistance zone | `horizontalParallelLines` |

zone 도구가 가격축 marker를 두 개 표시하므로 label에는 가격을 넣지 않는다.

```text
지지
저항 · 매물대
월봉 저항
주봉 지지 · 역할 전환
```

local/macro가 0.5 ATR 안에서 중복되면 higher-TF가 자동 승리하지 않는다. 더 최근의
confirmed reaction과 quality가 높은 하나를 남기고 MTF confluence metadata를 합친다.

## 7. Event state machine

이벤트는 단일 candle 조건 목록이 아니라 상태 전이로 계산한다.

### Breakout / breakdown

- 이전 close가 zone 안/반대편, signal close가 zone 경계 + 0.25 ATR 밖
- volume baseline은 signal candle을 제외한 과거 window로 계산
- relative volume 또는 dollar-volume participation이 없으면 low-priority candidate
- 이후 `hold`, `retest`, `failed` 상태를 asOf까지 추적
- failed breakout은 breakout Flag를 제거하거나 `실패한 돌파` 후보로 의미를 바꾼다.

### Retest

- wick이 이전 breakout zone에 접촉
- close가 breakout 방향으로 zone 밖에서 마감
- 다음 1~3봉 follow-through 또는 최소 hold를 확인한 뒤 확정
- level/zone ID와 breakout event ID를 모두 참조

### Gap

- previous close 대비 open gap ≥ 1 ATR
- `filled` 여부를 당일만이 아니라 asOf까지 추적
- 이미 오래전에 채워진 gap은 표시 후보가 아니다.
- unfilled/recent reaction zone과 현재 가격이 관련될 때만 candidate가 된다.

### 52주 high/low와 volume spike

- 연속 신고가/신저가는 episode 하나로 collapse하고 최신 representative candle만 사용
- volume spike 단독 Flag는 금지한다. level break, gap, reversal 같은 구조 사건을
  확인할 때만 표시한다.

## 8. Flag 선택

Flag hard gate:

- source timestamp exact membership
- display window 안 또는 현재 영향이 남은 unresolved event
- event age가 interval relevance window 안
- 관련 level/zone 또는 current structure 변화가 있음
- 동일 kind/ref episode 중복 아님

초기 relevance window:

| interval | 기본 최근 범위 |
| --- | ---: |
| 1D | 30 bars |
| 1W | 16 bars |
| 1M | 12 bars |

unfilled gap이나 unresolved role flip처럼 현재 상태가 살아 있으면 age 예외를 허용하되
그 이유를 quality metadata에 남긴다. Flag는 최신성보다 kind를 무조건 앞세우지 않고
`current impact → recency → evidence strength` 순으로 최대 2개 선택한다.
currentImpact와 evidenceStrength 수식은 Appendix A §6.2를 따른다.

## 9. 보유 데이터 활용

핵심 geometry는 canonical OHLCV만으로 완결한다. 추가 데이터는 confirmation 또는
counter-evidence로만 사용한다.

- candle volume/relative volume/dollar volume: breakout/event 참여도
- candle-derived volume profile: level/zone confluence, `estimated` 명시
- `order_flow_profile_daily`: 1D에서 coverage가 있을 때만 optional confirmation.
  estimated side classification을 표시하고 geometry를 단독 생성하지 않는다.
- 1W/1M order-flow는 충분한 daily coverage가 있을 때만 bounded aggregate한다.
- 뉴스·재무·peer는 이번 offline chart asset geometry에 넣지 않는다.

optional source failure는 후보를 만들지 못하게 하지 않고 해당 confirmation을 neutral로 둔다.

## 10. 자동 지표

`volume-profile`과 `volume` always-on 호환은 유지한다. recommended indicator는 rule이
증명 가능한 경우 최대 1개만 켠다.

| finding | 추천 |
| --- | --- |
| confirmed squeeze/compression | Bollinger |
| momentum transition이 구조 break와 동행 | MACD |
| extreme RSI가 실제 level reaction과 동행 | RSI |

단순 threshold만 넘었다고 켜지 않으며 LLM은 새 지표를 제안하지 않는다. 해설 focus item에
“왜 켰는지/무엇을 볼지”가 없으면 자동 활성화하지 않는다.
정확한 수식과 tie-break는 Appendix A §10.3을 따른다.

## 11. 결정론적 해설 fallback

fallback도 전체 feature 상위값을 다시 고르지 않는다. 최종 accepted drawing과 selected
evidence만 입력으로 받아 05의 commentary 구조를 조립한다. drawing이 없으면 데이터 상태와
“현재 품질 기준을 통과한 구조 작도 없음”을 명확히 말한다.

## 12. 테스트

- 1-touch VP level reject와 fresh-extreme 예외
- 연속봉 touch가 episode 1개로 계산됨
- 방향성 reaction/role flip/failed level
- near vs far level 선택, 슬롯 채우기 없음
- H-Line/zone label에 anchor 가격 token 없음
- breakout volume baseline에서 signal bar 제외
- confirmed retest와 failed breakout
- later-filled gap 제거
- repeated 52w high collapse
- Flag current-impact/recency ordering과 exact candle center
- optional VP/order-flow no-data가 geometry를 발명하지 않음
- recommended indicator 0~1개와 commentary reference
