# S3 Platform Contract

Current AWS bucket:

```text
gops-market-data-<aws-account-id>-ap-northeast-2-an
```

S3 stores optional archive artifacts. It is not the chart serving source and is not a prerequisite for backfill/gapfill.

Default development namespace:

```text
market-data/dev/helixho/...
```

Runtime writers:

- clickhouse-loader: closed candle archive under `S3_FINAL_PREFIX` after ClickHouse insertion succeeds
- backfill workers: processed candle archive under `S3_BACKFILL_PROCESSED_PREFIX` after ClickHouse materialization succeeds

Runtime writers write to ClickHouse first. S3 archive is best-effort evidence/recovery material, so S3 write failure must not block chart rendering when ClickHouse materialization succeeds.

The ClickHouse loader buffers post-insert candle archive writes by row count/time so the archive does not create one object per realtime candle. Disable this optional archive with `CLICKHOUSE_LOADER_S3_ARCHIVE_ENABLED=false` when testing ClickHouse-only runtime behavior.

There is no Kafka-to-S3 worker in the runtime path. New archive writers must preserve the same post-ClickHouse-success contract.

Leave `S3_ENDPOINT_URL` empty for real AWS S3.
Use the compose `local-s3` profile only for MinIO experiments.
