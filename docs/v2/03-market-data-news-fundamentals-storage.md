# Market Data / News / Fundamentals / Storage Role Notes

Use `../CHART_DATA_ARCHITECTURE.md` as the active chart-data contract and
`../CHART_DATA_OPERATIONS.md` for operational procedures.
Use current `systems/market-data/`, `platform/`, and `infra/` files for runtime
behavior.

Active chart-data rules:

- Do not preload a fixed universe or old legacy symbol set.
- Fetch chart ranges on demand from Redis, ClickHouse, S3 final/manifest, then
  Alpaca backfill.
- Store only the newest 120 candles per `symbol + timeframe` in Redis.
- Keep live provisional candles and latest closed replacements in Redis.
- Store confirmed candles in ClickHouse and S3 final/manifest.
- Keep raw Alpaca S3 backup out of chart-serving and materialization logic.
- Subscribe quotes only for the same explicit symbols that receive realtime
  trades; quote payloads flow through `market.layer.quotes.v1` to ClickHouse
  `quote_ticks`. Processed S3 final keeps candles/events, not high-volume
  trade/quote ticks.
- Preserve Kafka `key=symbol` ordering and avoid splitting one symbol partition
  across multiple pods.

News, fundamentals, orders, auth, and agent topics outside chart data should
continue to follow their current system contracts and task-specific docs.
