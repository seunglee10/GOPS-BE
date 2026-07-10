# Workstream 03: Event-first Realtime Delivery

## Goal

Use pub/sub and WebSocket as the steady-state live path, reduce Redis reads to bounded recovery, and
make order-flow quote joins and frontend refreshes stable across interval changes and rebalances.

## Structural problem

The hub subscribes to chart events and then reads all live Redis keys every 250 ms
(`systems/api-server/pods/api-server/gops-backend/app/market_data/realtime/stream_hub.py:104-119,161-194`).
For one symbol/interval this is 20 Redis commands/s while idle. The frontend additionally refreshes
candles every 15 seconds (`apps/gops-frontend/src/components/ChartPanel.tsx:613-639`) and refetches
the same interval-independent order-flow minutes when only bidask interval changes (`:918-976`).
The in-memory trade/quote join relies on implicit Kafka assignment behavior.

## Alternatives

1. Keep polling and increase Redis capacity. Preserves duplication and scales with active sessions.
2. Event-first delivery with startup snapshot and slow recovery poll. Uses current contracts and
   bounds missed-event recovery.
3. Redis Streams with per-session cursors. Durable delivery, but adds a new storage/consumer contract
   without evidence that pub/sub plus recovery is insufficient.

## Decision and tradeoffs

Choose option 2. A five-second recovery window is acceptable because live events still arrive by
pub/sub; the poll is only for missed events. Worst-case recovery rises from 250 ms to five seconds,
but normal latency and Redis load improve substantially.

## Change specification

- Send one Redis live snapshot on WS subscribe/reconnect.
- Separate pub/sub listening from a fake-clock-driven five-second recovery task.
- Recovery command budget for `S` symbols and `I` active symbol/interval pairs becomes
  `(2*S + 3*I)/5` commands/s, versus `4*(2*S + 3*I)` now.
- Disable the 15-second REST refresh while WS is healthy. Trigger REST only on initial load,
  reconnect/gap, explicit backfill completion, or stale heartbeat.
- Make order-flow intraday data keyed by symbol/session, not chart interval; interval changes reuse
  the same minute map and only recompute display buckets.
- Cache bucket results by bucket start plus contributing minute versions, so one minute update
  invalidates only 1 affected 1m/10m/1h bucket.
- Configure Kafka `RangePartitionAssignor` explicitly for the main trade+quote consumer and test that
  equal symbol keys map to the same member for both 12-partition topics.
- On quote-cache miss after startup/rebalance, load Redis at most once per symbol per one-second
  negative/positive cache window. Never perform a Redis GET per trade.
- Add focused fake-clock/session tests in `systems/api-server/tests/test_market_data_realtime.py`;
  keep Kafka/quote command-budget tests with market-data.

### [CONTRACT-CHANGE CC-2] migration

Repository search finds no consumer of `market.layer.candles.live.v1`. Release A adds
`PUBLISH_LIVE_CANDLE_TOPIC` and defaults it true. An operator verifies consumer groups. Release B
defaults it false while Redis pub/sub/WS remains unchanged. Release C removes producer code/topic
inventory after retention drains. Broker deletion is operator-owned and never part of this Goal.

## Query contract evaluation

`/api/charts/candles`, `/api/charts/order-flow/intraday`, `/ws/charts`, and event payloads are
unchanged. Kafka live-candle topic publication is the only external contract change.

## Acceptance criteria

1. Fake-clock idle test proves one-symbol/one-interval recovery reads fall from 20/s to <=1/s after
   the startup snapshot.
2. Every pub/sub event is delivered once without waiting for recovery; a deliberately dropped event
   is recovered within five seconds.
3. Healthy WS sessions issue no periodic REST candle request.
4. Switching bidask 1m/10m/1h issues no new intraday fetch and preserves rendered totals.
5. One minute update recomputes exactly one bucket for each cached interval.
6. N cache-miss trades in one second cause at most one Redis quote load per symbol.
7. Visual snapshots and existing order-flow command-count tests pass.

## Validation commands

```bash
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m unittest discover -s systems/api-server/tests -p 'test_market_data_realtime.py'
PYTHONPATH=systems/market-data/shared .venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_orderflow_redis_lean.py'
(cd apps/gops-frontend && npm run test:chart)
(cd apps/gops-frontend && npm run test:chart-visual)
git diff --check
```

Then run the full gate.

## Rollback

Restore `REALTIME_REDIS_POLL_SECONDS=0.25`, re-enable steady REST refresh, and set
`PUBLISH_LIVE_CANDLE_TOPIC=true`. These switches do not require data migration.

## Not doing

- No Redis Streams or new WS event.
- No Kafka partition-count change.
- No chart-axis or bidask rendering change.
