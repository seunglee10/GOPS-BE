# CDC: Chart Data Contract

## Purpose

GOPS market-data backend provides chart-engine input as layer-based market data.
The chart engine should build visualizations and indicators by combining these
layers instead of depending on separate backend APIs for each chart type.

Current delivery scope:

- Historical and latest candle windows are served through chart APIs.
- Realtime updates are delivered through chart WebSocket payloads.
- Realtime subscription targets are decided by backend subscription ownership:
  active chart symbol, watchlist, portfolio, and ranking cohorts.
- The chart engine receives normalized market data and decides how to render
  candles, overlays, indicators, tick layers, quote layers, and event markers.

## General Rules

| Rule | Contract |
| --- | --- |
| Symbol | Uppercase market symbol, e.g. `NVDA`, `AAPL` |
| Timestamp | ISO-8601 UTC string |
| Layer | One of `candles`, `trades`, `quotes`, `events` |
| Ordering | Apply payloads by `symbol + timestamp`; candle replacement uses `symbol + timeframe + timestamp` |
| Realtime update | WebSocket payloads may update the same chart point more than once |
| Derived indicators | SMA, EMA, WMA, Bollinger Bands, RSI, MACD, ATR, etc. are calculated by chart engine from layer data |

## Notes For Chart Engine

- `candles` is the main source for candle chart, line chart, moving averages,
  volume chart, VWAP, Bollinger Bands, RSI, and MACD.
- Moving averages are not delivered as a separate layer. Calculate them from
  candle fields, usually `close`.
- A candle with `isClosed=false` is provisional. Replace it when a payload with
  the same `symbol + timeframe + timestamp` arrives with `isClosed=true`.
- `trades` and `quotes` are realtime-heavy layers. Keep them bounded in memory.
- Alpaca stock quotes are top-of-book bid/ask only, not a 10-level order book.
- Alpaca trades do not provide a guaranteed aggressor side. Bid/ask order-flow
  delta can only be estimated by combining `trades` and `quotes`.

---

## Layer: candles

Used for candle chart, line chart, volume chart, moving averages, VWAP,
Bollinger Bands, RSI, MACD, and most price/volume indicators.

### Example Payload

```json
{
  "layer": "candles",
  "symbol": "NVDA",
  "timeframe": "1m",
  "timestamp": "2026-07-03T13:30:00.000Z",
  "open": 197.1,
  "high": 198.2,
  "low": 196.8,
  "close": 197.9,
  "volume": 120000,
  "vwap": 197.54,
  "tradeCount": 352,
  "isClosed": false
}
```

### Field Contract

| Field | Meaning | Expected Chart Usage | Required |
| --- | --- | --- | --- |
| `layer` | Payload layer name. Always `candles` | Routing to candle layer | Yes |
| `symbol` | Market symbol | Instrument selection | Yes |
| `timeframe` | Candle interval, e.g. `1m`, `5m`, `10m`, `1h`, `4h`, `1D`, `1W`, `1M` | Chart interval | Yes |
| `timestamp` | Candle bucket start time | X-axis / candle identity | Yes |
| `open` | First price in candle bucket | Candle body | Yes |
| `high` | Highest price in candle bucket | Candle wick / indicators | Yes |
| `low` | Lowest price in candle bucket | Candle wick / indicators | Yes |
| `close` | Last price in candle bucket | Candle body, line chart, SMA, EMA, RSI, MACD, Bollinger Bands | Yes |
| `volume` | Total traded size in candle bucket | Volume bars, volume indicators | Recommended |
| `vwap` | Volume-weighted average price for bucket | VWAP line or reference price | Recommended |
| `tradeCount` | Number of trades in bucket | Liquidity/activity display | Optional |
| `isClosed` | `false` for provisional candle, `true` for confirmed candle | Replacement/update logic | Yes |

### Derived Chart Examples

| Chart / Indicator | Required Data | Calculation Owner |
| --- | --- | --- |
| Candle chart | `open`, `high`, `low`, `close` | Chart engine |
| Line chart | `close` | Chart engine |
| Volume chart | `volume` | Chart engine |
| SMA | `close` window | Chart engine |
| EMA | `close` window | Chart engine |
| WMA | `close` window | Chart engine |
| Bollinger Bands | `close` window + standard deviation | Chart engine |
| VWAP | `vwap` or price/volume fallback | Chart engine |
| RSI | `close` changes | Chart engine |
| MACD | EMA from `close` | Chart engine |

### Other Notes

For realtime rendering, provisional candles should be updated in-place. Do not
append a duplicate candle when the same `symbol + timeframe + timestamp` appears.

---

## Layer: trades

Used for tick chart, trade tape, latest trade marker, volume profile, and
price-level volume analysis.

### Example Payload

```json
{
  "layer": "trades",
  "symbol": "NVDA",
  "tradeId": "123456",
  "price": 197.66,
  "size": 100,
  "exchange": "V",
  "conditions": ["@"],
  "tape": "C",
  "timestamp": "2026-07-03T13:30:01.120Z"
}
```

### Field Contract

| Field | Meaning | Expected Chart Usage | Required |
| --- | --- | --- | --- |
| `layer` | Payload layer name. Always `trades` | Routing to trade layer | Yes |
| `symbol` | Market symbol | Instrument selection | Yes |
| `tradeId` | Provider trade identifier | Dedupe / trace | Recommended |
| `price` | Executed trade price | Tick chart, last price, volume profile price bucket | Yes |
| `size` | Executed quantity | Trade tape, volume profile, price-level volume | Yes |
| `exchange` | Reporting exchange code | Tape details / debug | Optional |
| `conditions` | Trade condition codes | Filtering / display annotations | Optional |
| `tape` | Tape code | Market data diagnostics | Optional |
| `timestamp` | Trade event time | X-axis / ordering | Yes |

### Derived Chart Examples

| Chart / Indicator | Required Data | Calculation Owner |
| --- | --- | --- |
| Tick chart | `price`, `timestamp` | Chart engine |
| Trade tape | `price`, `size`, `timestamp`, `exchange` | Chart engine |
| Latest price marker | Latest `price` | Chart engine |
| Volume profile | `price`, `size` | Chart engine |
| Price-level volume | `price`, `size` | Chart engine |
| Order-flow profile | `trades` + `quotes` | `market_data.orderflow`, estimated only |

### Other Notes

Do not assume `trades` has a reliable buy/sell aggressor field. If directional
classification is needed, estimate it by comparing trade price with nearby quote
bid/ask or mid price.

---

## Layer: quotes

Used for bid/ask lines, spread chart, mid price, quote imbalance, and trade
direction estimation.

### Example Payload

```json
{
  "layer": "quotes",
  "symbol": "NVDA",
  "bidPrice": 197.64,
  "bidSize": 4,
  "askPrice": 197.66,
  "askSize": 7,
  "bidExchange": "V",
  "askExchange": "V",
  "conditions": ["R"],
  "timestamp": "2026-07-03T13:30:01.050Z"
}
```

### Field Contract

| Field | Meaning | Expected Chart Usage | Required |
| --- | --- | --- | --- |
| `layer` | Payload layer name. Always `quotes` | Routing to quote layer | Yes |
| `symbol` | Market symbol | Instrument selection | Yes |
| `bidPrice` | Best bid price | Bid line, spread, mid price | Yes |
| `bidSize` | Best bid size | Quote imbalance | Recommended |
| `askPrice` | Best ask price | Ask line, spread, mid price | Yes |
| `askSize` | Best ask size | Quote imbalance | Recommended |
| `bidExchange` | Best bid exchange code | Quote detail / debug | Optional |
| `askExchange` | Best ask exchange code | Quote detail / debug | Optional |
| `conditions` | Quote condition codes | Filtering / display annotations | Optional |
| `timestamp` | Quote event time | X-axis / ordering | Yes |

### Derived Chart Examples

| Chart / Indicator | Required Data | Calculation Owner |
| --- | --- | --- |
| Bid line | `bidPrice` | Chart engine |
| Ask line | `askPrice` | Chart engine |
| Spread chart | `askPrice - bidPrice` | Chart engine |
| Mid price | `(bidPrice + askPrice) / 2` | Chart engine |
| Quote imbalance | `bidSize / (bidSize + askSize)` | Chart engine |
| Trade side estimate | `quotes` + `trades` | Chart engine |

### Other Notes

Quotes are top-of-book only. The chart engine must not treat this as full depth
order-book data.

---

## Layer: events

Used for chart markers and market-state annotations such as trading status,
LULD, halt/resume, corrections, and cancellations.

### Example Payload

```json
{
  "layer": "events",
  "symbol": "NVDA",
  "eventType": "LULD",
  "status": "limit_down",
  "message": "Limit down pause",
  "timestamp": "2026-07-03T13:35:00.000Z"
}
```

### Field Contract

| Field | Meaning | Expected Chart Usage | Required |
| --- | --- | --- | --- |
| `layer` | Payload layer name. Always `events` | Routing to event marker layer | Yes |
| `symbol` | Market symbol or market-wide marker symbol | Instrument or market marker selection | Yes |
| `eventType` | Event category, e.g. `LULD`, `halt`, `resume`, `status`, `correction`, `cancel` | Marker style / filter | Yes |
| `status` | Event status value | Marker label / state display | Optional |
| `message` | Human-readable event detail | Tooltip / inspector | Optional |
| `timestamp` | Event time | X-axis marker position | Yes |

### Derived Chart Examples

| Chart / Marker | Required Data | Calculation Owner |
| --- | --- | --- |
| LULD marker | `eventType`, `timestamp`, `status` | Chart engine |
| Trading halt marker | `eventType`, `timestamp` | Chart engine |
| Resume marker | `eventType`, `timestamp` | Chart engine |
| Correction marker | `eventType`, `timestamp`, `message` | Chart engine |
| Status timeline | `eventType`, `status`, `timestamp` | Chart engine |

### Other Notes

Events may be sparse and should be rendered as markers or timeline annotations.
They should not be interpreted as price data.

---

## Minimal Layer Combination Guide

| Desired Feature | Layers Needed |
| --- | --- |
| Basic candle chart | `candles` |
| Candle + moving averages | `candles` |
| Candle + Bollinger Bands | `candles` |
| Candle + volume | `candles` |
| VWAP overlay | `candles` |
| Tick chart | `trades` |
| Trade tape | `trades` |
| Volume profile | `trades` |
| Bid/ask spread | `quotes` |
| Mid price | `quotes` |
| Order-flow estimated delta | `trades`, `quotes` |
| Halt/LULD markers | `events` |

---

## Helix Frontend Compatibility

The `helix/front` workspace UI reads chart data through the current GOPS backend
routes instead of a separate mock server. Keep these routes compatible when
changing the frontend or chart-serving layer:

| Usage | Route |
| --- | --- |
| Symbol list | `GET /api/charts/symbols` |
| Candle window | `GET /api/charts/candles` |
| Live candles | `WS /ws/charts?symbol={symbol}&interval={interval}` |

The candle query uses `symbol`, `interval`, `limit`, optional `before`,
optional `from`/`to`, and optional `ma`. The frontend currently sends
`session=regular` and `ma=5,20,60`; the backend may ignore unsupported optional
query parameters as long as the response remains compatible.

The frontend consumes candles in ascending timestamp order:

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

Realtime chart messages should be normalized to the existing chart WebSocket
contract. The helix frontend handles `LIVE_CANDLE_UPDATE`, `CANDLE_CLOSED`, and
`CANDLE_CORRECTED` by replacing a matching candle timestamp or appending a newer
candle. TreeMap data remains a static frontend seed in this merge; live TreeMap
summary data is a later backend integration task.
