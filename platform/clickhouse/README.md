# ClickHouse Platform Contract

ClickHouse is the confirmed historical serving store for chart data.
Redis keeps only latest 120 candles and live state; S3 final/manifest is the
durable rebuild source.

## Current Chart Tables

```text
market_data.chart_candles
market_data.trade_ticks
market_data.quote_ticks
market_data.market_events
market_data.market_status_events
market_data.volume_profile_bins_1m
market_data.backfill_jobs
market_data.storage_object_audit
market_data.load_audit
```

## Excluded From The Rebuild Contract

```text
market_data.market_quotes
```

Quote layer payloads are persisted to `market_data.quote_ticks`. Raw S3 backup
objects must not be loaded into ClickHouse by the normal chart path.
