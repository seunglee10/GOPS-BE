# 차트 데이터 계약 제안

## 백엔드 요청

### 1. 종목 목록 조회

```text
GET /api/charts/symbols
```

```ts
type ChartSymbolDto = {
  symbol: string;
  name: string;
  sector?: string;
  isMock?: boolean;
};

type ChartSymbolsResponseDto = {
  symbols: ChartSymbolDto[];
};
```

| 필드 | 필수 | 의미 | 사용처 |
| --- | --- | --- | --- |
| `symbols` | 예 | 차트에서 선택 가능한 종목 목록 | 종목 선택 |
| `symbol` | 예 | 차트 API에 전달할 종목 symbol | 캔들 조회, 실시간 캔들 |
| `name` | 예 | 사용자에게 보여줄 회사명 | 종목 선택, 차트 헤더 |
| `sector` | 아니오 | 종목 분류 | 종목 선택 |
| `isMock` | 아니오 | 더미/시연 종목 여부 | 로컬 시연 |

#### 비고

- 프론트는 하드코딩된 종목 목록 대신 이 API의 `symbols`를 우선 사용한다.
- 실제 차트 데이터 백엔드에서는 `isMock`을 생략할 수 있다.

### 2. 캔들 조회

```text
GET /api/charts/candles
```

```ts
type CandleQueryRequestDto = {
  symbol: string;
  interval: "1m" | "5m" | "10m" | "1D" | "1W" | "1M";
  limit: number;
  before?: string; // ISO UTC, exclusive
  from?: string;   // ISO UTC, inclusive
  to?: string;     // ISO UTC, exclusive
  session?: "regular";
  ma?: number[];   // example: [5, 20, 60]
};
```

`GET` query에서는 `ma=5,20,60` 형식으로 전달한다.

| 필드 | 필수 | 의미 | 사용처 |
| --- | --- | --- | --- |
| `symbol` | 예 | 조회할 종목 symbol | 캔들 차트, 거래량 막대, 이동평균선, 비교선 |
| `interval` | 예 | candle 시간 단위 | 캔들 차트, 거래량 막대, 이동평균선, 비교선 |
| `limit` | 예 | 반환받을 최대 candle 수 | 캔들 차트, 거래량 막대, 이동평균선, 비교선 |
| `before` | 아니오 | 해당 timestamp 이전 candle 조회 | 과거 구간 로드 |
| `from` | 아니오 | 조회 구간 시작 timestamp | 명시 구간 로드 |
| `to` | 아니오 | 조회 구간 종료 timestamp | 명시 구간 로드 |
| `session` | 아니오 | 조회할 market session. 기본값은 `regular` | 캔들 차트, 거래량 막대, 이동평균선, 비교선 |
| `ma` | 아니오 | 요청할 이동평균 window 목록 | 이동평균선 |

#### 조회 모드

| 모드 | 요청 필드 | 의미 |
| --- | --- | --- |
| 최신 조회 | `symbol`, `interval`, `limit` | 최신 candle부터 최대 `limit`개 조회 |
| 과거 조회 | `symbol`, `interval`, `limit`, `before` | `before` 이전 candle을 최대 `limit`개 조회 |
| 명시 구간 조회 | `symbol`, `interval`, `limit`, `from`, `to` | `[from, to)` 범위 안의 candle 조회 |

#### 비고

- `before`와 `from`/`to`는 같은 요청에서 함께 사용하지 않는다.
- 백엔드는 interval별 최대 `limit`과 최대 조회 기간을 정하고 초과 요청을 거절한다.
- `session`이 없으면 `regular` session candle을 반환한다.
- `5m`, `10m`, `1W`, `1M`의 저장/집계 방식은 백엔드가 결정한다. 응답 `interval`은 항상 요청한 interval과 같아야 한다.
- 요청 범위 안에서 사용 가능한 candle, 누락 candle, backfill 필요 여부는 백엔드가 판단한다.
- 디깅 영역 조회와 디깅으로 인한 interval 전환 조회는 `from`/`to` 명시 구간 조회를 사용한다.

### 3. 캔들 응답

```ts
type CandleQueryResponseDto = {
  symbol: string;
  interval: "1m" | "5m" | "10m" | "1D" | "1W" | "1M";
  request: {
    limit: number;
    before?: string;
    from?: string;
    to?: string;
    session: "regular";
  };
  status: "ready" | "partial" | "empty" | "pending" | "error";
  candles: CandleDto[];
  hasMoreBefore?: boolean;
  hasMoreAfter?: boolean;
  retryAfterMs?: number;
  error?: {
    code: string;
    message: string;
    retryable: boolean;
  };
};
```

| 필드 | 필수 | 의미 | 사용처 |
| --- | --- | --- | --- |
| `symbol` | 예 | 응답 candle의 종목 symbol | 캔들 차트, 거래량 막대, 이동평균선, 비교선 |
| `interval` | 예 | 응답 candle의 시간 단위 | 캔들 차트, 거래량 막대, 이동평균선, 비교선 |
| `request` | 예 | 백엔드가 처리한 요청 조건 | 캔들 차트, 거래량 막대, 이동평균선, 비교선 |
| `status` | 예 | 요청 구간의 데이터 준비 상태 | 캔들 차트, 거래량 막대, 이동평균선, 비교선 |
| `candles` | 예 | 요청 조건에서 반환 가능한 candle 배열 | 캔들 차트, 거래량 막대, 이동평균선, 비교선 |
| `hasMoreBefore` | 아니오 | 더 오래된 candle 존재 여부 | 과거 구간 로드 |
| `hasMoreAfter` | 아니오 | 더 최신 candle 존재 여부 | 명시 구간 로드 |
| `retryAfterMs` | 아니오 | `pending` 상태에서 재조회 권장 대기 시간 | 캔들 차트, 거래량 막대, 이동평균선, 비교선 |
| `error` | 아니오 | `error` 상태의 오류 정보 | 캔들 차트, 거래량 막대, 이동평균선, 비교선 |

#### 상태 값

| 값 | 의미 |
| --- | --- |
| `ready` | 요청 조건의 candle을 렌더링 가능한 수준으로 반환 |
| `partial` | 요청 조건 중 일부 candle만 반환 |
| `empty` | 요청 조건에 반환할 candle 없음 |
| `pending` | 요청 조건의 데이터 준비 또는 backfill 진행 중 |
| `error` | 요청 처리 실패 |

#### 비고

- `candles`는 `timestamp` 오름차순으로 반환한다.
- 같은 `timestamp`의 candle은 하나만 반환한다.
- `candles`에는 응답 요청 조건 밖의 candle을 포함하지 않는다.
- `partial`은 반환 가능한 candle이 있지만 요청 조건 전체를 채우지 못한 상태다.
- `pending`은 백엔드가 데이터 준비 또는 backfill을 시작했거나 진행 중인 상태다.
- `pending` 응답에는 가능하면 `retryAfterMs`를 포함한다.
- `error` 응답에는 `error.code`, `error.message`, `error.retryable`을 포함한다.

### 4. 캔들

```ts
type CandleDto = {
  timestamp: string; // ISO UTC
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  isClosed: boolean;

  ma5?: number;
  ma20?: number;
  ma60?: number;
};
```

| 필드 | 필수 | 의미 | 사용처 |
| --- | --- | --- | --- |
| `timestamp` | 예 | candle bucket timestamp, ISO UTC | 캔들 차트, 거래량 막대, 이동평균선, 비교선, 실시간 차트 |
| `open` | 예 | candle 시작 가격 | 캔들 차트, 실시간 차트 |
| `high` | 예 | candle 최고 가격 | 캔들 차트, 실시간 차트 |
| `low` | 예 | candle 최저 가격 | 캔들 차트, 실시간 차트 |
| `close` | 예 | candle 종가 | 캔들 차트, 비교선, 실시간 차트 |
| `volume` | 예 | candle 거래량 | 거래량 막대, 실시간 차트 |
| `isClosed` | 예 | candle 확정 여부 | 실시간 차트 |
| `ma5` | 조건부 | 5개 candle 이동평균 | 이동평균선 |
| `ma20` | 조건부 | 20개 candle 이동평균 | 이동평균선 |
| `ma60` | 조건부 | 60개 candle 이동평균 | 이동평균선 |

#### timestamp bucket

| interval | timestamp |
| --- | --- |
| `1m` | 해당 1분 bucket 시작 시각 |
| `5m` | 해당 5분 bucket 시작 시각 |
| `10m` | 해당 10분 bucket 시작 시각 |
| `1D` | 해당 거래일의 UTC 00:00 |
| `1W` | 해당 주 월요일의 UTC 00:00 |
| `1M` | 해당 월 1일의 UTC 00:00 |

#### interval 집계 규칙

| interval | 기준 |
| --- | --- |
| `1m` | `regular` session에 속한 원천 candle |
| `5m` | 같은 UTC bucket에 속한 `regular` session `1m` candle 집계 |
| `10m` | 같은 UTC bucket에 속한 `regular` session `1m` candle 집계 |
| `1D` | 해당 거래일 `regular` session의 `1m` candle 집계 |
| `1W` | 해당 주의 거래일 `1D` candle 집계 |
| `1M` | 해당 월의 거래일 `1D` candle 집계 |

상위 interval의 OHLCV는 하위 candle에서 아래 방식으로 계산한다.

| 필드 | 집계 방식 |
| --- | --- |
| `open` | bucket 안 첫 candle의 `open` |
| `high` | bucket 안 `high`의 최댓값 |
| `low` | bucket 안 `low`의 최솟값 |
| `close` | bucket 안 마지막 candle의 `close` |
| `volume` | bucket 안 `volume` 합계 |

#### 비고

- `open`, `high`, `low`, `close`, `volume`은 finite number여야 한다.
- `ma5`, `ma20`, `ma60`은 요청한 이동평균이 계산 가능한 candle에만 포함한다.
- 이동평균 계산에 필요한 lookback candle은 백엔드가 내부적으로 조회한다.
- lookback candle은 응답 `candles`에 포함하지 않는다.
- 이동평균을 계산할 수 없는 candle에는 해당 MA 필드를 생략한다.
- `session=regular`는 미국 주식 정규장 기준 `America/New_York` 09:30부터 16:00 전까지의 거래 구간이다.
- `1D`, `1W`, `1M`의 timestamp는 UTC bucket을 사용하지만, OHLCV 값은 `regular` session candle만 집계한다.

### 5. 실시간 캔들

```text
WS /ws/charts?symbol={symbol}&interval={interval}
```

```ts
type CandleEventDto = {
  type: "LIVE_CANDLE_UPDATE" | "CANDLE_CLOSED" | "CANDLE_CORRECTED";
  symbol: string;
  interval: "1m" | "5m" | "10m" | "1D" | "1W" | "1M";
  data: CandleDto;
};
```

| 필드 | 필수 | 의미 | 사용처 |
| --- | --- | --- | --- |
| `type` | 예 | 실시간 candle event 종류 | 실시간 차트 |
| `symbol` | 예 | event 대상 종목 | 실시간 차트 |
| `interval` | 예 | event 대상 interval | 실시간 차트 |
| `data` | 예 | 갱신할 candle | 실시간 차트 |

#### 비고

- WebSocket event의 `symbol`과 `interval`은 구독 요청과 일치해야 한다.
- WebSocket event의 `data`는 REST 응답의 `CandleDto`와 같은 shape을 사용한다.
- 실시간 event도 요청 interval 기준 candle이어야 한다.
- `1m`, `5m`, `10m`, `1D`, `1W`, `1M` 실시간 candle은 같은 원천 거래 흐름에서 집계되어야 한다.
- higher interval 실시간 aggregation을 지원하지 못하는 경우 해당 interval 구독은 명시적으로 실패해야 한다.

### 6. 기타 고려

| 항목 | 내용 |
| --- | --- |
| 데이터 레벨 | `candle`, `trade`, `trade_quote` 데이터 레벨은 백엔드가 관리한다. |
| 프론트 책임 | 프론트는 차트 API를 호출하고, 응답 상태에 따라 렌더링 가능 여부를 판단한다. |
| 데이터 없음 | 필요한 데이터 레벨이 서버에 없으면 백엔드는 해당 차트 API에서 `empty`, `pending`, `error` 중 적절한 상태로 응답한다. |
| S&P500 candle | S&P500 전체 분봉/일봉 수집은 백엔드 내부 정책으로 관리한다. |
| 관심종목 trade | 관심종목 등록/삭제는 session-scoped `trade` 필요 intent를 갱신하는 trigger다. |
| 관심종목 quote | 관심종목이라고 해서 `trade_quote`까지 필요하다고 보지 않는다. |
| session TTL | 접속 종료 또는 TTL 만료 시 해당 session의 `trade` intent는 정리한다. |
| hot ranking | 실시간 상위 종목의 `trade` 필요 여부는 백엔드가 내부적으로 관리한다. |
| trade_quote 대상 | POC의 `trade_quote` 수집 대상은 백엔드 config 또는 하드코딩으로 관리한다. |
| trade_quote API | 프론트는 별도 `trade_quote` intent API를 호출하지 않는다. `trade_quote` 기반 차트 API를 호출하면 백엔드가 데이터 가능 여부를 판단한다. |

## 차트별 렌더링 계획

### 캔들 차트

#### WHAT

가격의 시작가, 고가, 저가, 종가를 하나의 candle로 표현한다.

#### WHY

사용자가 특정 시간 구간의 가격 움직임과 변동폭을 가장 기본적인 형태로 읽기 위해 사용한다.

#### HOW

`timestamp`로 x축 위치를 잡고, `open`, `close`로 candle body를 그린다. `high`, `low`로 wick을 그린다.

| 데이터 | 사용API | 활용 |
| --- | --- | --- |
| `timestamp` | `GET /api/charts/candles`, `WS /ws/charts` | candle x축 위치 |
| `open` | `GET /api/charts/candles`, `WS /ws/charts` | body 시작 가격 |
| `high` | `GET /api/charts/candles`, `WS /ws/charts` | wick 상단과 y축 범위 |
| `low` | `GET /api/charts/candles`, `WS /ws/charts` | wick 하단과 y축 범위 |
| `close` | `GET /api/charts/candles`, `WS /ws/charts` | body 종료 가격 |
| `isClosed` | `GET /api/charts/candles`, `WS /ws/charts` | 진행 중 candle과 확정 candle 구분 |

---

### 시점 디깅

#### WHAT

선택한 candle 구간을 하위 interval candle로 펼쳐서 같은 차트 안에 표시한다.

#### WHY

사용자가 큰 시간 단위에서 발견한 움직임을 같은 맥락 안에서 더 작은 시간 단위로 확인하기 위해 사용한다.

#### HOW

루트 candle 클릭은 현재 interval을 유지하고 하위 interval을 디깅 영역으로 연다. 디깅 영역 안의 candle 클릭은 전체 차트 interval을 해당 candle의 interval로 전환하고, 클릭한 candle을 다시 하위 interval로 연다.

```text
1M -> 1W
1W -> 1D
1D -> 10m
10m -> 1m
5m -> 1m
1m -> footprint
```

| 데이터 | 사용API | 활용 |
| --- | --- | --- |
| `timestamp` | `GET /api/charts/candles` | 디깅 대상 candle 식별, interval 전환 후 중심 시간축 |
| `from` | `GET /api/charts/candles` | 디깅 구간 시작 |
| `to` | `GET /api/charts/candles` | 디깅 구간 종료 |
| `interval` | `GET /api/charts/candles` | 하위 candle 조회 interval |
| `open`, `high`, `low`, `close`, `volume` | `GET /api/charts/candles` | 디깅 영역의 candle/거래량 렌더링 |

---

### 거래량 막대

#### WHAT

각 candle 구간의 거래량을 막대로 표현한다.

#### WHY

가격 움직임에 동반된 거래 강도를 확인하기 위해 사용한다.

#### HOW

`timestamp`로 x축 위치를 맞추고, `volume`으로 막대 높이를 계산한다.

| 데이터 | 사용API | 활용 |
| --- | --- | --- |
| `timestamp` | `GET /api/charts/candles`, `WS /ws/charts` | 거래량 막대 x축 위치 |
| `volume` | `GET /api/charts/candles`, `WS /ws/charts` | 막대 높이 |

---

### 이동평균선

#### WHAT

가격의 단기, 중기, 장기 흐름을 선으로 표현한다. 현재 차트는 `MA5`, `MA20`, `MA60`을 사용한다.

#### WHY

사용자가 가격의 방향성과 추세 변화를 빠르게 파악하기 위해 사용한다.

#### HOW

`timestamp`로 x축 위치를 맞추고, `ma5`, `ma20`, `ma60` 값을 각각 선으로 연결한다.

| 데이터 | 사용API | 활용 |
| --- | --- | --- |
| `timestamp` | `GET /api/charts/candles`, `WS /ws/charts` | 이동평균 point의 x축 위치 |
| `ma5` | `GET /api/charts/candles`, `WS /ws/charts` | 단기 이동평균선 |
| `ma20` | `GET /api/charts/candles`, `WS /ws/charts` | 중기 이동평균선 |
| `ma60` | `GET /api/charts/candles`, `WS /ws/charts` | 장기 이동평균선 |

---

### 드로잉 도구

#### WHAT

프론트엔드에서 차트 위에 수평선, 추세선, 세로마커, 텍스트, 포인트, 화살표, 범위, 측정을 그린다.

#### WHY

사용자가 차트 위에서 직접 기준 가격, 구간, 사건, 측정값을 표시하기 위해 사용한다.

#### HOW

`timestamp`로 x축 anchor를 잡고, 화면 y좌표를 price로 변환해 drawing anchor를 만든다. 드로잉 상태는 프론트엔드가 보관한다.

| 데이터 | 사용API | 활용 |
| --- | --- | --- |
| `timestamp` | `GET /api/charts/candles`, `WS /ws/charts` | drawing x축 anchor |
| `high` | `GET /api/charts/candles`, `WS /ws/charts` | price scale 범위 |
| `low` | `GET /api/charts/candles`, `WS /ws/charts` | price scale 범위 |
| `close` | `GET /api/charts/candles`, `WS /ws/charts` | price scale 및 기준값 |

---

### 비교선

#### WHAT

현재 종목과 비교 종목의 가격 변화를 같은 시간축에서 비교한다.

#### WHY

사용자가 시장 안에서 특정 종목이 다른 종목보다 강한지 약한지 비교하기 위해 사용한다.

#### HOW

현재 종목과 비교 종목의 `timestamp`를 맞추고, 각 종목의 `close`를 기준값 대비 percentage로 변환해 선으로 연결한다.

| 데이터 | 사용API | 활용 |
| --- | --- | --- |
| `symbol` | `GET /api/charts/candles` | 비교 대상 종목 구분 |
| `timestamp` | `GET /api/charts/candles` | 현재 종목과 비교 종목의 시간 정렬 |
| `close` | `GET /api/charts/candles` | 기준값 대비 percentage 계산 |

---

### 실시간 차트

#### WHAT

새로운 candle update를 기존 차트에 반영한다.

#### WHY

사용자가 보고 있는 차트를 최신 시장 상태에 가깝게 유지하기 위해 사용한다.

#### HOW

`CandleEventDto.data.timestamp`가 기존 candle과 같으면 교체하고, 최신 candle보다 새로우면 추가한다. `CANDLE_CORRECTED`는 같은 timestamp candle을 교체한다.

| 데이터 | 사용API | 활용 |
| --- | --- | --- |
| `type` | `WS /ws/charts` | update, close, correction 구분 |
| `symbol` | `WS /ws/charts` | 현재 차트 종목과 event 일치 확인 |
| `interval` | `WS /ws/charts` | 현재 차트 interval과 event 일치 확인 |
| `data.timestamp` | `WS /ws/charts` | 교체 또는 추가할 candle 식별 |
| `data.open` | `WS /ws/charts` | 실시간 candle body 시작값 |
| `data.high` | `WS /ws/charts` | 실시간 wick 상단 |
| `data.low` | `WS /ws/charts` | 실시간 wick 하단 |
| `data.close` | `WS /ws/charts` | 실시간 candle body 종료값 |
| `data.volume` | `WS /ws/charts` | 실시간 거래량 막대 갱신 |
| `data.isClosed` | `WS /ws/charts` | 진행 중 candle과 확정 candle 구분 |

## 기타

### 병합 규칙

| 대상 | 규칙 |
| --- | --- |
| snapshot candle | `timestamp` 기준으로 기존 candle과 병합 |
| 같은 timestamp snapshot candle | 새 snapshot candle 우선 |
| live event 같은 timestamp | 기존 candle 교체 |
| live event 새 timestamp | 기존 최신 candle보다 새로우면 추가 |
| live event 오래된 unknown timestamp | 무시 |
