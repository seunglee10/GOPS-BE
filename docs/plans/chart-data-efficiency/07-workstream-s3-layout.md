# Workstream 06: Sharded S3 Realtime Layout

## Goal

Change realtime/raw S3 object creation from symbol-proportional to bounded time-window/shard
proportional while preserving candle rebuild results and existing historical backfill objects.

## Structural problem

Both sinks buffer by symbol/day (`systems/market-data/shared/alfaka/storage/processed_s3_sink.py:104-146`;
`raw_s3_archive_sink.py:94-130`) and write a data object plus manifest per flush
(`processed_s3_sink.py:215-266`; `raw_s3_archive_sink.py:151-174`). With 502 configured symbols and a
10-second K8s flush, a single 1m wave can create 1,004 PUTs; repeating it gives a 60,240 PUT/hour
processed lower bound. Longer flush intervals reduce frequency but retain the symbol-linear curve.

## Alternatives

1. Raise flush interval/count only. Easy rollback, but leaves one small object per active symbol and
   increases data-loss/recovery windows.
2. Group realtime rows into fixed time windows and 32 deterministic symbol shards. Bounds sparse
   object creation and preserves Parquet/filterable rows.
3. Replace S3 sinks with Firehose/Iceberg. May solve compaction, but adds new infrastructure and
   table-management scope not justified by current consumers.

## Decision and tradeoffs

Choose option 2. Listing an hour/shard prefix and filtering symbol rows is more CPU/GET work than a
single symbol prefix, but realtime object PUTs fall by roughly 31x for the measured sparse wave:
`502*60*2 = 60,240` v1 PUTs versus at most `32*60 = 1,920` v2 data PUTs per sink replica. High-rate
trade/quote batches may create more objects, but never one buffer merely because a symbol exists.

## Change specification

- Shard: `crc32(upper(symbol)) % 32`, formatted `00..31`, with cross-language fixture tests.
  Events without a symbol use normalized `_MARKET`.
- Processed realtime key:
  `final-v2/candles/interval={I}/date={YYYY-MM-DD}/hour={HH}/shard={SS}/part-{window}-{digest}.parquet`.
- Processed event key:
  `final-v2/events/type={T}/date={YYYY-MM-DD}/hour={HH}/shard={SS}/part-{window}-{digest}.parquet`.
- Raw key:
  `raw-v2/alpaca/channel={C}/date={YYYY-MM-DD}/hour={HH}/shard={SS}/part-{window}-{digest}.jsonl`.
- Use UTC one-minute flush windows. Object digest covers the canonical serialization of all sorted
  rows so exact replay writes the same key. The raw format remains JSONL; only its partitioning
  changes. Readers
  dedupe `sourceEventId` across non-identical replay batches.
- Do not write one manifest per realtime object. Readers compute day/hour/shard prefixes and list
  bounded windows. Historical/on-demand `final/.../symbol=.../request=...` objects and manifests stay v1.
- K8s and compose process only closed candles/events in processed final. Trades/quotes remain raw S3
  plus bounded ClickHouse; remove compose-only final tick subscriptions.
- Expose rows/object, objects/window/shard, PUT retries, list count, and duplicate-row counters.
- Split focused fixture tests into `test_processed_s3_sink.py`, `test_raw_s3_archive.py`, and
  `test_s3_manifest.py`; no test may resolve real AWS endpoints.

### [CONTRACT-CHANGE CC-4] migration

1. Deploy dual-reader (`v1 + v2`, dedupe by canonical identity) before any v2 writer.
2. Enable v2 shadow writes for fixture/local MinIO only; compare reconstructed candles byte-for-byte.
3. In deployment, dual-write for one operator observation window, then set realtime writer to v2 only.
4. Keep historical/backfill v1 writes unchanged. Keep old realtime v1 objects readable until their
   lifecycle window expires; never rename in place.

## Query contract evaluation

Public APIs and candle payloads do not change. S3 is an external storage contract, so every lookup,
rebuild, repair, and manifest tool must be dual-read before writer cutover. The layout is documented
as a storage adapter contract, not exposed as a new API.

## Acceptance criteria

1. Fixture reconstruction from v1, v2, and mixed layouts yields identical ordered candle rows.
2. One 502-symbol 1m wave creates <=32 v2 data objects per sink replica and zero per-object manifests.
3. Object count depends on nonempty shards/windows, not distinct symbols.
4. Replay of an identical batch writes identical object keys; mixed-batch duplicates are removed by
   canonical identity during read/rebuild.
5. Backfill/request v1 object and manifest tests remain unchanged.
6. MinIO/local compose tests use fixtures only and make no Alpaca/AWS request.

## Validation commands

```bash
PYTHONPATH=systems/market-data/shared .venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_processed_s3_sink.py'
PYTHONPATH=systems/market-data/shared .venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_raw_s3_archive.py'
PYTHONPATH=systems/market-data/shared .venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_s3_manifest.py'
docker compose config
docker compose build
git diff --check
```

Then run the full gate and visual snapshots because S3 reconstruction feeds candle queries.

## Rollback

Set writer to v1 and leave dual-reader enabled. V2 objects are immutable and harmless. Do not delete
either prefix during rollback.

## Not doing

- No S3 table format, Firehose, or cross-bucket migration.
- No change to historical/backfill deterministic manifests.
- No production object deletion by the agent.
