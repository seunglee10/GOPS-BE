# GOPS v2 Architecture Notes

Chart-data details have moved to short current contracts so this broader v2
document cannot override runtime behavior.

For active contracts, use:

- `../CHART_DATA_ARCHITECTURE.md` for chart data ownership and query contracts.
- `../CHART_DATA_OPERATIONS.md` for validation, recovery, and rollout.
- `../PRODUCT_CONTEXT.md` for product direction.
- `../STRUCTURE_GUIDE.md` for repository structure.
- `../ARCHITECTURE.md`, `../ENVIRONMENT.md`, and `../IMAGE_STRATEGY.md` for
  current runtime, image, deployment, and platform boundaries.
- Current code under `systems/`, `apps/`, `platform/`, and `infra/`.

Retired chart assumptions that must not be reintroduced:

- fixed legacy symbol set or broad S&P500 historical chart preload
- broad S&P500 trades/quotes tick collection
- legacy raw/tick/candle topic families from the pre-rebuild design
  chart topics
- Redis keys such as `price:{symbol}:latest`,
  `candle:{symbol}:{interval}:live`, or `candles:{symbol}:{interval}`
- S3 live prefixes or raw S3 as a chart-serving/materialization source
- quotes as all-symbol durable storage

The current chart rebuild starts with empty history, may use a SIP S&P500
bars/statuses baseline for recent 1m entry, subscribes only explicit realtime
trade/quote symbols, attaches quotes only to the same realtime trade symbols,
writes quote live state to Redis/WebSocket, persists canonical quote payloads
through `market.layer.quotes.v1`, and serves history through Redis latest 120
candles, ClickHouse, S3 final/manifest, then Alpaca backfill.
