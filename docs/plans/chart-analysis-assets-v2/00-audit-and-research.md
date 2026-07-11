# 00. 구현 감사와 조사 근거

이 문서는 v2 설계의 출발점이다. 로컬 상태는 재현 스냅샷이며, 외부 연구 결과는 GOPS
품질 gate의 가설을 세우는 근거다. 다른 시장·주기에서 나온 결과를 그대로 일반화하지
않고 08의 actual US-equity walk-forward/canary로 다시 검증한다.

## 1. 2026-07-11 로컬 NVDA 재현

### 저장 자산

```text
NVDA 1D asset
asOf        2026-07-06T00:00:00.000Z
generatedAt 2026-07-10T20:54:53.984Z
status      degraded
payload     19,816 bytes

Layer T
trendLine 2025-09-17 @ 168.41 -> 2025-09-25 @ 173.12
label     상승 추세선 (접점 2)

Layer S
H-Line 129.93, 136.15, 140.28, 169.28, 183.42
Flag   2025-01-27 breakout, 2025-10-10 breakout, 2026-07-02 52wHigh
```

현재 chart API의 NVDA 1D 120봉은 `2026-01-16T05:00:00.000Z`부터
`2026-07-10T04:00:00.000Z`까지였다. 저장 자산의 timed anchor 5개는 현재 candle
timestamp와 exact match가 모두 실패했다. 같은 거래일이 존재한 2026-07-02 Flag도
asset은 `00:00Z`, chart candle은 `04:00Z`였다.

이 관찰은 두 문제를 분리한다.

1. 오래된 anchor가 현재 입력 범위와 무관한 **분석 품질 문제**
2. 같은 거래일의 timestamp가 다른 **데이터 identity/렌더 정렬 문제**

하나만 고치면 충분하지 않다.

## 2. 코드상 원인

### 데이터·anchor

- `chart_assets/candles.py`는 `ClickHouseMarketDataProvider.daily_candles`를 직접 읽는다.
- `clickhouse_provider.py`의 1D 집계는 `toStartOfDay(event_time)`으로 `00:00Z`를 만든다.
- canonical serving merge는 `provider.py:merge_timestamp_key`에서 뉴욕 시장일 자정을
  UTC `04:00/05:00Z`로 바꾼다.
- builder coverage는 행 개수만 기록하고, expected exchange session, 가장 긴 결측,
  최근 연속 구간, serving renderability를 확인하지 않는다.
- 프런트는 asset DrawingEntity를 그대로 add하고 timestamp 문자열 exact match가 없으면
  시간 구간 연속 보간으로 조용히 fallback한다.

### 구조 레이어

- level `touches`는 독립 touch episode가 아니라 cluster 안 pivot 개수다.
- 절대 방향 없는 향후 가격 이동을 reaction으로 점수화하고, dwell 없는 close-side 전환도
  role flip으로 센다.
- VP confluence 하나가 1-touch level을 표시 threshold 위로 올릴 수 있다.
- compiler는 아래 3/위 2를 고른 뒤 부족한 슬롯을 5개까지 채우며 current ATR distance와
  최근성은 보지 않는다.
- Flag는 종류 우선순위가 최신성보다 앞서고, ref가 없는 반복 `52wHigh`를 collapse하지 않는다.

### 추세 레이어

- 연속 same-kind pivot 두 점이면 후보가 될 수 있고 독립적인 세 번째 접점을 요구하지 않는다.
- 접점을 candle close proximity로 세어 실제 wick touch와 연속 인접봉을 구분하지 않는다.
- 이후 침범만 볼 뿐 현재 가격과의 거리, 마지막 접점 나이, projection 수명을 보지 않는다.
- 접점 동률이면 더 가파른 slope가 우선한다.
- channel은 signed slope가 아니라 절댓값을 비교한다. 현재 후보 모델은 low line은 양의 slope,
  high line은 음의 slope만 만들기 때문에 실제 평행채널보다 수렴 wedge를 channel로 오인한다.
- 후보가 없으면 통계적 Range 여부와 무관하게 최근 고저 Range를 반드시 반환한다.

### Layer I와 해설

- prompt는 lookback의 pivot/level/event 전체를 보낸다.
- LLM은 tool과 anchor 조합을 만들며 compiler는 존재·개수·timestamp·예산만 검증한다.
- 현재 관련성, 같은 구조의 조합인지, 시간 순서, 중복 ID, 침범, fit 품질은 검증하지 않는다.
- confidence는 LLM 자기평가를 그대로 저장한다.
- 숫자 grounding은 `commentary.text`만 경고 수준으로 검사하고 label/rationale/keyLevels/
  invalidation은 빠진다.
- LLM intent를 compiler가 drop한 뒤 commentary를 다시 맞추지 않는다.
- 1M 해설 첫 문장을 1W/1D prompt에 넣어 LLM 오류가 하위 주기로 전파될 수 있다.

## 3. 외부 근거에서 가져올 것

### 체계적·검증 가능한 패턴

[Lo, Mamaysky, Wang의 NBER 연구](https://www.nber.org/papers/w7613)는 주관적인 차트
패턴을 자동 알고리즘과 통계적 비교로 평가했다. GOPS에 적용할 핵심은 특정 패턴의
수익률 약속이 아니라, **모양을 본 사람의 직감 대신 재현 가능한 알고리즘과 표본 밖
평가가 필요하다**는 점이다.

### 지지·저항의 touch와 decay

[Chung & Bellotti](https://arxiv.org/abs/2101.07410)는 과거 bounce 횟수가 많은 SR
level에서 재반응 가능성이 높고 시간이 지나며 그 효과가 감소하는 현상을 보고했다.
따라서 GOPS level은 독립 bounce episode와 recency decay를 핵심으로 두고, 오래된
1-touch level을 슬롯 채우기로 살리지 않는다. 이 연구는 intraday 자료이므로 v2의
1D/1W/1M threshold는 별도 평가로 보정한다.

[New York Fed의 order 연구](https://www.newyorkfed.org/research/staff_reports/sr125.html)는
FX의 stop/take-profit 주문이 round number에 군집하고 support/resistance·돌파와 연결될
수 있음을 보였다. GOPS에서는 round number를 약한 보조 확인으로만 유지한다. FX 결과를
미국 주식의 독립 hard evidence로 취급하지 않는다.

[Technical Analysis and Liquidity Provision](https://academic.oup.com/rfs/article-abstract/17/4/1043/1570736)는
support/resistance와 limit-order-book depth의 관련성을 보고했다. GOPS의 candle-derived
VP와 estimated order-flow는 보조 확인으로 쓸 수 있지만, geometry hard gate를 대신하지
않고 quality provenance를 노출한다.

### salient pivot와 robust fit

[SciPy peak prominence 정의](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.peak_prominences.html)는
peak가 주변 baseline에서 얼마나 두드러지는지를 측정한다. 라이브러리를 추가하지 않고
같은 개념의 bounded ATR-normalized prominence를 pure Python으로 구현한다.

[Fischler & Bolles의 RANSAC 원 논문](https://graphics.stanford.edu/courses/cs164-10-spring/Handouts/papers_RANSAC.pdf)은
gross error가 섞인 점 집합에서 consensus set으로 모델을 검증하는 접근을 제시한다.
GOPS는 난수 RANSAC을 그대로 쓰지 않고, salient pivot 상위 K의 pair를 결정론적으로
열거한 뒤 ATR residual consensus를 구한다. 재현성과 낮은 부하를 동시에 지킨다.

### 과최적화 방지

[Bailey 등](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)은 금융 backtest의
과최적화 위험과 단순 hold-out의 한계를 다룬다. v2 threshold는 NVDA 한 사례에 맞추지
않고 episode 분리, walk-forward, 변경 횟수 기록을 사용한다. 수익률은 보조 지표일 뿐
“현재 구조를 유의미하게 표현하는가” rubric을 대체하지 않는다.

### LLM 계약과 비용

[OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)는
JSON schema 준수를 제공한다. 이는 값의 투자적 의미까지 검증한다는 뜻이 아니므로,
GOPS는 strict schema 위에 candidate-ID·cross-reference·quality validator를 둔다.

[Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create)와
[data controls](https://developers.openai.com/api/docs/guides/your-data)에 따라 offline
자산 호출은 `store:false`를 명시한다. 응답 본문은 저장하지 않고 model/usage/latency와
선택된 candidate audit만 ClickHouse asset에 남긴다.

[Batch API](https://developers.openai.com/api/docs/guides/batch)는 비대화형 대량 처리의
비용·rate-limit 이점이 있지만, v2 첫 구현은 현재 수동 동기 worker 흐름을 유지한다.
품질 안정 후 전체 S&P500 운영 지표가 전환을 정당화할 때만 별도 계획으로 검토한다.

## 4. 조사 결과를 설계로 번역하는 규칙

| 근거 | v2 적용 | 적용하지 않는 과장 |
| --- | --- | --- |
| systematic pattern recognition | versioned pure kernel, golden/walk-forward | 패턴 이름만으로 방향 예측 |
| repeated bounce + decay | 독립 touch episode, recency | 오래된 touch를 영구 유효 취급 |
| order/round-number clustering | 약한 confluence | FX 결과를 주식 hard gate로 사용 |
| prominence | salient pivot 선별 | SciPy 신규 dependency |
| robust consensus | deterministic bounded pair consensus | 난수·무제한 O(P²) 탐색 |
| backtest overfitting | episode 분리, threshold change log | 단일 NVDA에 threshold 튜닝 |
| strict structured output | candidate-ID schema | schema 준수 = 의미 정확성이라고 가정 |

## 5. 변경 전 반드시 고정할 baseline

구현 묶음 A 시작 시 read-only 진단 script로 다음을 JSON artifact가 아닌 테스트 로그에
남긴다.

- NVDA/AAPL 1D·1W 자산별 drawings, timed anchor exact-match 수, current distance ATR
- layer별 drawing 수와 payload bytes
- 실제 canonical candle coverage/digest
- prompt bytes/tokens와 symbol당 호출 수
- 현재 test suite 결과

API key, raw prompt, raw OpenAI response는 baseline에 기록하지 않는다.
