# 03. 집계 검증: NVDA 편향 판별 진단

## 목표

NVDA 오더플로우가 한쪽(ask 또는 bid)에 치우쳐 보이는 현상이 **라이브 분류 경로의
아티팩트인지, 실제 시장 특성인지** 판별하는 재사용 가능한 진단을 만든다.
**이 문서는 분류 로직 수리를 전제하지 않는다** — 수리는 진단 결과가 아티팩트로
판정된 경우에만, 별도 합의 후 진행한다(사용자 결정).

## 배경: 분류 경로가 두 개이고 품질이 다르다

같은 quote-rule(`classify_trade_side`,
`systems/market-data/shared/alfaka/orderflow/classification.py:115-146`)을 쓰지만:

| | 라이브 경로 (오늘, Redis) | EOD 롤업 경로 (과거, ClickHouse) |
| --- | --- | --- |
| 위치 | `alfaka/streaming/processor.py:596-614` | `alfaka/orderflow/rollup.py` |
| 호가 결합 | `PinnedQuoteCache` — `live:quote:{symbol}` 최신 스냅샷 1개, 150ms 메모이즈 (`orderflow/quote_cache.py:16-37`), 최대 2000ms 낡은 호가 허용 | `merge_trades_with_quotes` — 시간 정렬 as-of join (`classification.py:89-112`) + 개장 전 5분 워밍업 |
| 이론적 위험 | 급변 구간에서 낡은 NBBO와 비교된 체결 묶음이 같은 쪽으로 쏠림. Kafka 처리 순서상 quote가 trade보다 늦거나 빠를 수 있음 | 원료 틱이 완전하면 정확 |

또한 분류가 불가능하면 `unknown`으로 분리 적재되므로(한쪽으로 기본값 처리 없음),
`unknownVolume` 비율 자체가 호가 공급 품질의 지표다.

## 진단 도구 사양

`scripts/local/orderflow_verify.py` (또는 `alfaka.orderflow.verify` 모듈 + 얇은
스크립트)를 만든다. 기존 `rollup.py`의 조회·as-of join 코드를 재사용하고 새 저장은
하지 않는다(읽기 전용, `--dry-run` 성격).

### 입력

```text
--symbol NVDA --date YYYY-MM-DD [--minutes-detail] [--json]
```

### 비교 대상 3개

1. **live**: 당일 실행 시 Redis의 라이브 오더플로우 상태. 키 레이아웃은 02 적용
   전 `order-flow:{symbol}:live` 해시, 적용 후 `order-flow:{symbol}:minutes` ZSET +
   `live-minute`이므로, 저장 형태에 중립인 **API
   `GET /api/charts/order-flow/intraday` 응답을 소스로 쓰는 것을 권장**한다.
   당일이 지났으면 생략.
2. **asof-ticks**: ClickHouse `trade_ticks`+`quote_ticks`에서 as-of join으로 재계산
   (rollup의 `--source ticks` 경로를 dry-run 재사용).
3. **daily-row**: 이미 적재된 `order_flow_profile_daily`의 해당 세션 row (있다면).

### 산출 지표 (심볼·세션 단위 + `--minutes-detail`이면 분 단위)

```text
- totalVolume, askVolume, bidVolume, unknownVolume
- unknownRatio = unknown / total
- skew = (ask - bid) / max(1, ask + bid)          # -1..+1
- 라이브 vs asof-ticks: 분 단위 skew 상관계수, 분 단위 부호 불일치율,
  세션 합계 skew 차이(delta-skew)
- 호가 커버리지: quote_ticks의 분당 건수 분포(호가 공백 분 개수)
```

### 판정 규칙 (리포트에 명시 출력)

```text
A. asof-ticks 자체가 큰 skew (예: |skew| > 0.3) 이고 live와 유사
   → 시장 특성일 가능성 높음. 여러 세션(최근 5~10일)에서 반복되는지 확인.
      과거 daily-row들도 같은 방향이면 "정상, 수리 불필요"로 종결.
B. live와 asof-ticks가 유의미하게 다름 (delta-skew > 0.15 또는 부호 불일치율 > 25%)
   → 라이브 경로 아티팩트. 04 §5(인메모리 NBBO)를 as-of 버퍼로 확장하는 후속 수리를
      제안하고 사용자 합의 후 진행.
C. unknownRatio > 0.15
   → 호가 공급 문제(구독 누락/공백). 04 §7(구독 상한 분리)과 quote_ticks 공백 조사로 연결.
```

임계값(0.3 / 0.15 / 25% / 0.15)은 초기값이며 첫 실행 결과를 보고 조정한다. 조정 시
이 문서에 기록한다.

## 실행 계획

1. 스크립트 구현 + 로컬 compose 환경에서 동작 확인 (`.venv`, Python 3.12).
2. 프로덕션 ClickHouse를 향해 NVDA 최근 5~10 거래 세션에 대해 실행, 리포트 저장.
3. 판정 A/B/C에 따라 README의 결합 규칙대로 후속 진행.

## 참고: 이미 확인된 관련 사실 (조사 결과, 재검증 불필요)

- 분류는 호가 부재 시 `unknown` 처리라 "기본값 편향"은 없다 (`classification.py:115-146`).
- 핀 심볼은 trades+quotes 두 레이어 모두 구독된다
  (`alfaka/streaming/subscription_cohorts.py`의 order-flow source, layers `{trades,quotes}`).
- `ALPACA_MAX_TRADE_SYMBOLS` 상한이 quotes 채널에도 동일 적용되지만
  (`alfaka/alpaca/websocket_collector.py:531-541`), order-flow 소스는 우선순위 최상위라
  핀 5종목은 상한에서 잘리지 않는다. 비핀 종목의 unknown 폭증 위험은 04 §6에서 다룬다.

## 수용 기준

1. 스크립트가 한 세션에 대해 위 지표와 판정(A/B/C)을 stdout(+`--json`)으로 출력한다.
2. NVDA 최근 세션들에 대한 실행 리포트가 남고, 판정 결과가 README 결합 규칙에 따라
   후속 작업(수리 진행/종결)으로 연결된다.
3. 기존 코드(분류·롤업·서빙)는 이 단계에서 변경되지 않는다.
