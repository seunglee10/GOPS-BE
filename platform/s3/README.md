# S3 Platform Contract

S3 is durable market-data evidence. Only final objects and manifests participate
in chart serving, backfill coverage, and ClickHouse rebuild.

Raw Alpaca payloads may be written under `S3_RAW_PREFIX`, but that prefix is
backup-only. It must not be read by chart API, coverage checks, backfill
decisions, or ClickHouse loaders unless a future raw-replay pipeline is
explicitly designed.

Current AWS bucket:

```text
gops-market-data-<aws-account-id>-ap-northeast-2-an
```

Chart-data rewrite prefix contract:

```text
S3_RAW_PREFIX=market-data/rebuild-20260702-lazy-v1/raw/alpaca
S3_FINAL_PREFIX=market-data/rebuild-20260702-lazy-v1/final
S3_LIVE_PREFIX=market-data/rebuild-20260702-lazy-v1/live
S3_MANIFEST_PREFIX=market-data/rebuild-20260702-lazy-v1/manifest
S3_MATERIALIZE_PREFIX=market-data/rebuild-20260702-lazy-v1/final
```

Runtime writers:

- raw archive sink writes backup-only payloads under `S3_RAW_PREFIX`;
- processed/final sink writes confirmed data under `S3_FINAL_PREFIX`;
- live evidence writes under `S3_LIVE_PREFIX`;
- backfill workers write manifests under `S3_MANIFEST_PREFIX`.

Runtime readers:

- chart API reads Redis and ClickHouse, not S3 raw;
- backfill coverage checks S3 manifests/final objects, not S3 raw;
- ClickHouse materialization reads final objects, not S3 raw.

Final candle keys include `feed={sip|boats}` so SIP/BOATS overlap cannot
collapse into one object.

Leave endpoint values empty for real AWS S3:

```text
S3_ENDPOINT_URL=
DOCKER_S3_ENDPOINT_URL=
```

Use the compose `local-s3` profile only for MinIO experiments.
