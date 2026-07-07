# S3 Platform Contract

S3 has two roles in the chart rebuild:

- `final` and `manifest` are durable evidence and ClickHouse rebuild sources.
- `raw/alpaca` is optional backup-only storage and is outside the read path.

## Prefix Contract

```text
S3_RAW_PREFIX=market-data/rebuild-20260702-lazy-v1/raw/alpaca
S3_FINAL_PREFIX=market-data/rebuild-20260702-lazy-v1/final
S3_MANIFEST_PREFIX=market-data/rebuild-20260702-lazy-v1/manifest
S3_MATERIALIZE_PREFIX=market-data/rebuild-20260702-lazy-v1/final
```

Do not configure `S3_LIVE_PREFIX` for the rebuild path. Live candles belong in
Redis/WebSocket state, not S3. Quote layer payloads are stored under
`final/quotes` after the quote processor republishes them to
`market.layer.quotes.v1`.

## Final Objects

```text
market-data/rebuild-20260702-lazy-v1/final/candles/feed={feed}/interval={1m|5m|10m|1h|4h|1D|1W|1M}/symbol={symbol}/year=YYYY/month=MM/day=DD/*.parquet
market-data/rebuild-20260702-lazy-v1/final/trades/symbol={symbol}/year=YYYY/month=MM/day=DD/feed={feed}/*.parquet
market-data/rebuild-20260702-lazy-v1/final/quotes/symbol={symbol}/year=YYYY/month=MM/day=DD/feed={feed}/*.parquet
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
