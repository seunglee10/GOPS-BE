# Retired Market Data Tick-To-Candle Architecture

This document was retired for the on-demand chart-data rebuild.

Use `../CHART_DATA_REBUILD_PLAN.md` as the active source of truth for chart
Kafka topics, Redis keys, S3 paths, ClickHouse tables, backfill behavior,
SIP/BOATS routing, and frontend monitoring scope.

Current rebuild rules:

- Chart history starts empty; no preset historical S&P500, legacy fixed symbol set, or preload collection.
- The active runtime may keep a SIP S&P500 bars/statuses baseline, but not
  S&P500-wide trades/quotes.
- Kafka input topics use `market.input.realtime.*.v1`.
- Realtime tick fanout topics such as `market.realtime.ticks.to.1m.v1` are
  retained for legacy/debug use, but the default processor hot path handles raw
  trades directly and does not re-consume tick fanout.
- Live candles use `market.layer.candles.live.v1`; closed candles use
  interval-specific topics such as `market.layer.candles.1m.closed.v1`,
  `market.layer.candles.1h.closed.v1`, and
  `market.layer.candles.1mo.closed.v1`.
- Kafka messages use `key=symbol`; one partition is handled by one consumer pod
  at a time.
- Redis keeps latest 120 candles per `symbol + timeframe`, live provisional
  candles, latest closed candles, live trades, live quotes, live events,
  backfill state, subscription state, and SIP/BOATS feed state.
- Quotes are subscribed only for symbols that already receive realtime trades.
  The quote processor writes Redis/WebSocket live state and republishes
  canonical quote payloads to `market.layer.quotes.v1` for ClickHouse
  `quote_ticks` storage. Processed S3 final keeps candles/events, while raw S3
  archive remains the replay evidence path for high-volume trade/quote ticks.
- S3 raw is backup-only. Chart serving and materialization use ClickHouse and
  S3 final/manifest, never raw backup.
- SIP and BOATS are mutually exclusive by feed session.
