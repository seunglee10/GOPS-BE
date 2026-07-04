# Retired Market Data Tick-To-Candle Architecture

This document was retired for the on-demand chart-data rebuild.

Use `../CHART_DATA_REBUILD_PLAN.md` as the active source of truth for chart
Kafka topics, Redis keys, S3 paths, ClickHouse tables, backfill behavior,
SIP/BOATS routing, and frontend monitoring scope.

Current rebuild rules:

- Chart data starts empty; no preset S&P500, legacy fixed symbol set, or preload collection.
- Kafka input topics use `market.input.realtime.*.v1`.
- Realtime tick fanout uses per-timeframe topics such as
  `market.realtime.ticks.to.1m.v1`.
- Candle layer topics are timeframe-specific:
  `market.layer.candles.live.v1` and
  `market.layer.candles.closed.v1`.
- Kafka messages use `key=symbol`; one partition is handled by one consumer pod
  at a time.
- Redis keeps latest 120 candles per `symbol + timeframe`, live provisional
  candles, latest closed candles, live trades, live quotes, live events,
  backfill state, subscription state, and SIP/BOATS feed state.
- Quotes are subscribed only for symbols that already receive realtime trades.
  Quotes are Redis/WebSocket only; there is no quote layer topic, ClickHouse
  quote table, or S3 quote archive in the active path.
- S3 raw is backup-only. Chart serving and materialization use ClickHouse and
  S3 final/manifest, never raw backup.
- SIP and BOATS are mutually exclusive by feed session.
