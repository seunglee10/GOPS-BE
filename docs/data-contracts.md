# Data Contracts

이 문서가 Kafka, Redis, S3, ClickHouse 계약의 기준입니다.

## Symbols

기준 파일:

```text
config/market-data-request.json
```

기본 universe는 semiconductor-100입니다. `ALPACA_SYMBOLS` 환경변수가 있으면 운영에서는 그 값이 우선합니다.

## Alpaca Subscription

```text
default channels: bars, updatedBars
active chart channels: trades
```

동작:

```text
1. alpaca-ingestor는 기본으로 1분봉 계열만 구독한다.
2. frontend가 /ws/charts에 접속하면 backend가 Redis active key를 갱신한다.
3. alpaca-ingestor는 active key가 살아 있는 symbol만 trades를 추가 구독한다.
4. WebSocket이 닫히면 TTL이 만료되고 trades 구독이 해제된다.
```

## Kafka

Raw topics:

```text
market.raw.bars
market.raw.updated-bars
market.raw.trades
```

Processed topics:

```text
market.ticks.v1
market.candles.live.1m.v1
market.candles.closed.v1
```

## Redis

```text
price:{symbol}:latest                  latest trade price, TTL 1 day
candle:{symbol}:1m:live                live 1m candle from trades, TTL 1 day
candle:{symbol}:{interval}:latest      latest closed candle, TTL 1 day
candles:{symbol}:{interval}            recent closed candle sorted set, TTL 7 days
market.events                          Pub/Sub all chart events
market.events:{symbol}                 Pub/Sub symbol chart events
active:charts:symbols                  active symbol set
active:charts:{symbol}                 active symbol TTL marker
```

## S3

```text
market-data/raw/alpaca/source=alpaca/channel={channel}/symbol={symbol}/year=YYYY/month=MM/day=DD/hour=HH/
market-data/final/candles/interval={interval}/symbol={symbol}/year=YYYY/month=MM/day=DD/
market-data/live/candles/interval=1m/symbol={symbol}/year=YYYY/month=MM/day=DD/
market-data/live/trades/symbol={symbol}/year=YYYY/month=MM/day=DD/hour=HH/
```

Format:

```text
raw: jsonl
final/live processed: parquet snappy
```

Policy:

```text
전날까지 확정된 candle은 final에 저장한다.
오늘 candle/tick은 live에 저장한다.
오늘 tick 전체를 프론트 최초 진입 때 replay하지 않는다.
장마감 후 live/trades compact job을 별도로 둘 수 있다.
```

## ClickHouse

Schema file:

```text
infra/clickhouse/initdb/01-market-data.sql
```

Tables:

```text
market_data.chart_candles
market_data.trade_ticks
market_data.load_audit
```

Default loader:

```text
KAFKA_CLICKHOUSE_TOPICS=market.candles.closed.v1
CLICKHOUSE_LOAD_TRADES=false
```

GOPS chart API reads `chart_candles` first for historical candles and Redis for latest/live data.
