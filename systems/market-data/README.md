# Market Data System

Owns Alpaca ingest, stream processing, market-data storage, backfill, and serving helpers.

## Folders

```text
pods/market-ingestor/       Alpaca live WebSocket entrypoint
pods/market-processor/      local Python stream processor
pods/s3-sink/               processed Kafka topics to S3
pods/clickhouse-loader/     processed Kafka topics to ClickHouse
pods/backfill-worker/       Redis queued historical backfill worker
pods/market-processor/flink/ future Flink job contract for market processing
jobs/symbol-registry-sync/  symbol metadata sync job
config/                     market universe and subscription policy
shared/alfaka/              market-data import namespace
tests/                      market-data tests
```

## Runtime Entrypoints

```text
pods/market-ingestor/market_stream.py           wraps alfaka.alpaca.websocket_collector
pods/market-processor/local_main.py             wraps alfaka.streaming.processor
pods/s3-sink/processed_sink.py                  wraps alfaka.storage.processed_s3_sink
pods/clickhouse-loader/processed_loader.py      wraps alfaka.storage.clickhouse_loader
pods/backfill-worker/main.py                    wraps alfaka.backfill.worker
jobs/symbol-registry-sync/main.py               wraps alfaka.tools.sync_symbol_registry
```

## Images

```text
gops-market-ingestor    market-ingestor
gops-market-processor   market-processor, symbol-registry-sync
gops-market-storage     s3-sink, clickhouse-loader
gops-backfill-worker    backfill-worker
```

## Platform Dependencies

```text
Kafka
Redis
ClickHouse
S3
Secrets Manager / Alpaca credentials
```

Keep `alfaka.*` imports stable. Docker, compose, k8s, tests, and local scripts should place `systems/market-data/shared` on `PYTHONPATH`.
