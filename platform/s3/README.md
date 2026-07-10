# S3 Platform Contract

S3 has two roles in the chart data path:

- historical `final` and `manifest` are durable candle evidence and recovery sources;
- realtime `final-v2` and `raw-v2` use minute/hour/32-shard objects to bound PUT count.

`raw` and `raw-v2` are backup-only and expire after 30 days through Terraform.
No lifecycle rule expires `final`, `final-v2`, backfill, or manifest evidence.

## Prefix Contract

```text
S3_RAW_PREFIX=market-data/rebuild-20260702-lazy-v1/raw/alpaca
S3_FINAL_PREFIX=market-data/rebuild-20260702-lazy-v1/final
S3_MANIFEST_PREFIX=market-data/rebuild-20260702-lazy-v1/manifest
S3_MATERIALIZE_PREFIX=market-data/rebuild-20260702-lazy-v1/final
S3_REALTIME_LAYOUT_MODE=v2
```

Do not configure `S3_LIVE_PREFIX`. Live candles belong in Redis/WebSocket
state, not S3. Quote payloads are retained in raw/raw-v2 backup and
`market_data.quote_ticks`; processed final/final-v2 stores closed candles and
market events only.

## Realtime V2 Objects

```text
market-data/rebuild-20260702-lazy-v1/final-v2/candles/interval={interval}/date=YYYY-MM-DD/hour=HH/shard=00..31/part-{minute}-{digest}.parquet
market-data/rebuild-20260702-lazy-v1/final-v2/events/type={type}/date=YYYY-MM-DD/hour=HH/shard=00..31/part-{minute}-{digest}.parquet
market-data/rebuild-20260702-lazy-v1/raw-v2/alpaca/channel={channel}/date=YYYY-MM-DD/hour=HH/shard=00..31/part-{minute}-{digest}.jsonl
```

The shard is `crc32(UPPER(symbol)) % 32`. Rows are canonical-sorted and
deduplicated before a deterministic digest key is written. Buffers are keyed by
partition plus UTC minute, and exact replay skips an existing digest key after
`HEAD`. V2 has no per-object manifest. `dual` mode writes/reads both layouts during migration; current
Compose/K8s defaults to `v2`, while historical/backfill objects keep v1.

Readers filter LIST results by the minute encoded in the object key. A shared
shard object is fully materialized and audited, but it satisfies a fill only
when `matchedRowCount > 0` for the requested symbol, interval, and range.

## Historical V1 Objects

```text
market-data/rebuild-20260702-lazy-v1/final/candles/feed={feed}/interval={1m|5m|10m|1h|4h|1D|1W|1M}/symbol={symbol}/year=YYYY/month=MM/day=DD/*.parquet
market-data/rebuild-20260702-lazy-v1/final/events/event_type={status|unknown}/symbol={symbol}/year=YYYY/month=MM/day=DD/*.parquet
```

## Manifests

```text
market-data/rebuild-20260702-lazy-v1/manifest/candles/interval={interval}/symbol={symbol}/objects/{digest}.json
market-data/rebuild-20260702-lazy-v1/manifest/backfill/request={requestId}.json
```

## Raw Backup

```text
market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel={trades|quotes|bars|updated-bars|daily-bars|events}/symbol={symbol}/year=YYYY/month=MM/day=DD/*.jsonl
market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel={bars|daily-bars}/symbol={symbol}/request={requestId}/*.jsonl
```

Raw backup objects must not participate in chart serving, coverage checks,
backfill decisions, or ClickHouse loading. The raw archive consumer runs with
`KAFKA_RAW_S3_ENABLE_AUTO_COMMIT=false`; it commits offsets only after every S3
side effect in the flush succeeds. Failed uploads and invalid canonical events
must leave the offsets uncommitted for replay.

Terraform variables `manage_s3_chart_data_lifecycle`,
`acknowledge_s3_lifecycle_document_ownership`, `s3_chart_data_root_prefix`, and
`s3_raw_retention_days` control lifecycle ownership and retention. Management
defaults to false. An existing bucket requires explicit acknowledgement that
this module owns the complete bucket lifecycle document. Review
`terraform plan`; applying it is operator-owned.
