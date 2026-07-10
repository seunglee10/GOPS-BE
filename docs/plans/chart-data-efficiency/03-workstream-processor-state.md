# Workstream 02: Bounded Processor State

## Goal

Make stream-processor memory and persistent writes independent of session length while preserving
closed/live candle, fixed SMA, and order-flow output exactly.

## Structural problem

Six processor collections append without pruning (`systems/market-data/shared/alfaka/streaming/transforms.py:164-218,343-398,409-537,601-636`).
Every trade also writes a legacy tick VP bin (`systems/market-data/shared/alfaka/streaming/processor.py:607-620,1027-1032`),
although active VP reads candles (`systems/market-data/shared/alfaka/serving/provider.py:213-222`).
The issue is not one high-volume symbol; any long-lived processor grows state and writes at trade rate.

## Alternatives

1. Periodically restart processor pods. Bounds uptime, not state correctness, and creates rebalance
   risk.
2. Add explicit horizons to current builders and remove the unread writer. Smallest safe change.
3. Replace all state with Kafka Streams/Flink. Strong state primitives, but disproportionate to the
   current Python topology and a high migration risk.

## Decision and tradeoffs

Choose option 2. Persisted Redis/ClickHouse candle watermarks remain the recovery boundary. Rare
corrections targeting an evicted aggregate window are recomputed from canonical source candles
rather than retaining every source row forever. This adds a storage read on correction, but moves
cost from session length to the rare correction rate.

## Change specification

- `LiveCandleBuilder`: retain current and previous grace-window minute per symbol; prune on advance.
- `MovingAverageState`: retain a timestamp-ordered map capped at 60 per symbol/interval and recompute
  sums over that bounded window. This preserves current out-of-order correction and floating-order
  behavior while making cost constant in session length.
- `CandleAggregator`: retain open windows only. On completion, emit and evict. A corrected closed
  source row invokes a bounded recompute adapter for that one target window from canonical candles.
- `TickWindowCandleBuilder` and `CalendarCandleAggregator`: replace permanent `closed_keys` sets with
  TTL/LRU markers capped at 2,048 per builder; persisted watermarks reject older events.
- `SourceEventDeduper`: replace list + `pop(0)` with deque + set, retaining the 10,000 identity cap.
- Delete `VolumeProfileBinBuilder` from active processor state and stop the old Redis write.
- Add state gauges: entries by builder and evictions/recomputes by reason. Metrics must not write to
  Redis per message.

### [CONTRACT-CHANGE CC-1] migration

Release A stops the old VP writer behind `LEGACY_TICK_VP_WRITE_ENABLED=true` defaulting to false and
records any legacy-reader counter. Release B removes the flag, Redis key builder/provider method,
and docs after one compatibility window. Workstream 08 removes fresh-install DDL only after Release B.

## Query contract evaluation

Public API/WS/Kafka candle payloads remain byte-equivalent after volatile timestamps are normalized.
The legacy Redis VP key is the only contract change; no active API uses it.

## Acceptance criteria

1. A 100,000-minute multi-symbol fixture keeps every builder at its documented cap.
2. Current/prior-window late events and corrections produce the same candles as the old builders.
3. An older correction recomputes one bounded target window and does not resurrect historical state.
4. Fixed SMA and all candle payload golden fixtures remain equal.
5. N trades issue zero legacy VP Redis commands; existing order-flow command budgets still pass.
6. Processor restart/recovery tests pass with bounded state.

## Validation commands

```bash
PYTHONPATH=systems/market-data/shared .venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_market_data_hardening.py'
PYTHONPATH=systems/market-data/shared .venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_orderflow_redis_lean.py'
PYTHONPATH=systems/market-data/shared .venv/bin/python -m unittest discover -s systems/market-data/tests
git diff --check
```

Then run the full gate.

## Rollback

Re-enable `LEGACY_TICK_VP_WRITE_ENABLED` only during Release A. Builder changes are one commit and can
be reverted without a storage migration because output contracts do not change.

## Not doing

- No consumer-framework migration.
- No special branch for NVDA or a particular session.
- No deletion of existing ClickHouse tables in this Goal.
