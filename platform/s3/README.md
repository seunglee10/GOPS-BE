# S3 Platform Contract

S3 has two roles in the chart rebuild:

- historical `final` and `manifest` are durable candle evidence and rebuild sources;
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

Do not configure `S3_LIVE_PREFIX` for the rebuild path. Live candles belong in
Redis/WebSocket state, not S3. Quote layer payloads are stored under
`final/quotes` after the quote processor republishes them to
`market.layer.quotes.v1`.

## Realtime V2 Objects

```text
market-data/rebuild-20260702-lazy-v1/final-v2/candles/interval={interval}/date=YYYY-MM-DD/hour=HH/shard=00..31/part-{minute}-{digest}.parquet
market-data/rebuild-20260702-lazy-v1/final-v2/events/type={type}/date=YYYY-MM-DD/hour=HH/shard=00..31/part-{minute}-{digest}.parquet
market-data/rebuild-20260702-lazy-v1/raw-v2/alpaca/channel={channel}/date=YYYY-MM-DD/hour=HH/shard=00..31/part-{minute}-{digest}.jsonl
```

The shard is `crc32(UPPER(symbol)) % 32`. Rows are canonical-sorted and
deduplicated before a deterministic digest key is written. V2 has no per-object
manifest. `dual` mode writes/reads both layouts during migration; current
Compose/K8s defaults to `v2`, while historical/backfill objects keep v1.

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
backfill decisions, or ClickHouse loading.

Terraform variables `manage_s3_chart_data_lifecycle`,
`s3_chart_data_root_prefix`, and `s3_raw_retention_days` control lifecycle
ownership and retention. Review `terraform plan`; applying it is operator-owned.
