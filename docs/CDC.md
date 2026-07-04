# Chart Data Contract

## 현재 전제

이 문서는 현재 프론트 차트가 읽는 데이터 형태를 정리한 보고서다. 백엔드 병합 과정에서 endpoint나 DTO가 달라질 수 있으며, 그 경우 `frontend/src/chart/cdcClient.ts`와 `frontend/src/chart/types.ts`를 함께 조정하면 된다.

프론트는 현재 같은 origin 기준으로 아래 경로를 사용한다.

| 용도 | 경로 |
| --- | --- |
| 종목 목록 | `GET /api/charts/symbols` |
| 캔들 조회 | `GET /api/charts/candles` |
| 실시간 캔들 | `WS /ws/charts?symbol={symbol}&interval={interval}` |

## TreeMap Data Note

현재 TreeMap은 chart candle API/WS와 연결되어 있지 않다. `frontend/src/market/sp500Universe.seed.ts`의 `sp500UniverseSeed`를 `TreeMapCanvas`에 넣어 정적 화면을 만든다.

실제 백엔드 연결 시 TreeMap은 S&P500 전체의 분봉 기반 summary/live 데이터로 교체하면 된다. 기본 데이터 성격은 `symbol`, `companyName`, `sector`, `industry`, `marketCap` 또는 `indexWeight`, `latestPrice`, `changePercent`, `updatedAt`이다.

`latestPrice`와 `changePercent`는 실시간 분봉 데이터로 갱신되는 summary에서 오면 된다. TreeMap은 기본적으로 trade tick stream을 직접 요구하지 않는다. endpoint와 DTO는 백엔드 병합 과정에서 자유롭게 정할 수 있으며, 변경 시 TreeMap data source와 `TreeMapCanvas` 입력을 함께 조정하면 된다.

## 종목 목록

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

`isMock`은 로컬 시연용 표시이며 실제 백엔드에서는 생략 가능하다.

## 캔들 조회

프론트 client는 다음 query를 만든다.

```ts
type CandleQuery = {
  symbol: string;
  interval: "1m" | "5m" | "10m" | "1D" | "1W" | "1M";
  limit: number;
  before?: string;
  from?: string;
  to?: string;
  ma?: number[];
};
```

현재 client는 `session=regular`와 `ma=5,20,60`을 기본으로 붙인다. `from`과 `to`가 함께 있으면 명시 구간 조회로 사용하고, `before`는 과거 구간 로드에 사용한다.

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

프론트는 `candles`를 timestamp 오름차순으로 정렬해 사용한다. `status`가 `empty`, `pending`, `error`이면 차트는 데이터 없음 또는 상태 메시지로 전환된다.

## CandleDto

```ts
type CandleDto = {
  timestamp: string;
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

`open`, `high`, `low`, `close`, `volume`은 finite number여야 한다. `ma5`, `ma20`, `ma60`은 표시 가능한 candle에만 포함하면 된다.

## 실시간 캔들

```ts
type CandleEventDto = {
  type: "LIVE_CANDLE_UPDATE" | "CANDLE_CLOSED" | "CANDLE_CORRECTED";
  symbol: string;
  interval: "1m" | "5m" | "10m" | "1D" | "1W" | "1M";
  data: CandleDto;
};
```

프론트는 event의 `symbol`과 `interval`이 현재 차트와 다르면 무시한다. 같은 `timestamp` candle은 교체하고, 최신 candle보다 새로운 timestamp는 추가한다.

## 차트 기능별 데이터 사용

| 기능 | 사용하는 데이터 |
| --- | --- |
| 캔들 차트 | `timestamp`, `open`, `high`, `low`, `close`, `isClosed` |
| 거래량 | `timestamp`, `volume` |
| 이동평균 | `timestamp`, `ma5`, `ma20`, `ma60` |
| 실시간 갱신 | `CandleEventDto.data` |
| 시점 디깅 | 선택 candle의 `timestamp`와 interval별 `from`/`to` 범위 |
| 드로잉 anchor | `timestamp`, price scale 산출용 `high`, `low`, `close` |

디깅 interval은 현재 프론트에서 아래 규칙을 사용한다.

```text
1M -> 1W
1W -> 1D
1D -> 10m
10m -> 1m
5m -> 1m
1m -> footprint placeholder
```

footprint는 현재 데이터 연결 없이 placeholder만 있다.
