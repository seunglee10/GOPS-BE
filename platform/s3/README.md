# S3 Platform Contract

Current AWS bucket:

```text
gops-market-data-<aws-account-id>-ap-northeast-2-an
```

S3 stores raw archives, processed/final market data, live artifacts, and replay/evidence material.

Runtime writers:

- raw S3 archive sink: raw Kafka topics under `S3_RAW_PREFIX`
- processed S3 sink: processed Kafka topics under `S3_FINAL_PREFIX` and `S3_LIVE_PREFIX`
- backfill workers: historical raw/archive and canonical processed materialization inputs

Historical raw backfill objects include a range/job-derived suffix in the object name so repeated or overlapping preload windows do not overwrite each other. Broad historical preload should use `S3_HISTORICAL_RAW_PARTITION_MODE=chunk` and `S3_HISTORICAL_PROCESSED_MANIFEST_LAYOUT=compact`; this keeps S3 evidence reusable without producing one tiny raw object per trading day. The v1 S&P 500 preload keeps `1D` on the 3-year range but caps `1m` initial-load at `BACKFILL_INITIAL_LOAD_1M_MIN_START=2025-04-01T00:00:00Z`, so March 2025 or older `1m` history is not fetched by Goal/deploy runs.

For S3-to-ClickHouse smoke tests, set `S3_MATERIALIZE_KEYS` to one or more explicit processed candle object keys. Use prefix-wide `S3_MATERIALIZE_PREFIX` only for intentional broad materialization.

Keep time-based flush enabled so low-volume raw/status partitions do not remain only in worker memory.

Leave `S3_ENDPOINT_URL` empty for real AWS S3.
Use the compose `local-s3` profile only for MinIO experiments.
