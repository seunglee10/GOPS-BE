# Milestone 4: Backfill And GapFill Reliability

Status: closed for local S3-first v1 implementation, 2026-06-30

## Implemented Locally

- Redis Streams is now the default backfill queue backend.
- Backfill status records include job metadata: `jobType`, `sourcePreference`, `idempotencyKey`, `attempt`, `claimedBy`, `claimedAt`, `heartbeatAt`, `checkpoint`, and `streamId`.
- Worker flow now uses consumer-group read, claim metadata, terminal-job ack, stale reclaim, retry limits, and dead-letter stream handling.
- `initial_load` and `gapfill` jobs use the existing Alpaca historical `1m`/`1D` runner path.
- `coverage-first` runner execution now checks ClickHouse covered range/internal gaps, processed S3 manifest entries, bounded exact S3 partitions, then Alpaca.
- Processed candle S3 writes emit per-object manifest entries under `S3_MANIFEST_PREFIX`.
- Historical raw S3 archive writes emit raw manifest entries under `S3_MANIFEST_PREFIX`.
- Bounded GapFill jobs can query deduped ClickHouse candle timestamps and fetch only coalesced missing source ranges.
- A first GapFill helper coalesces missing canonical `1m` / `1D` buckets and can skip weekends, configured closed dates, and configured early closes.
- Coverage repair now passes leading missing coverage ranges from chart coverage metadata into queued backfill requests.
- `replay_repair` and `correction_replay` now execute from processed S3 objects or raw S3 candle archives and materialize through the canonical ClickHouse materializer.
- Replay jobs intentionally do not call Alpaca; `sourcePreference=alpaca-only` is rejected for replay/correction execution.
- `RedisBackfillStore.queue_metrics()` and `GET /api/charts/backfill/queue` expose stream length, pending count, undelivered lag, backlog count, oldest pending entry, and dead-letter length.
- Materializer retry tests cover the v1 partial-failure contract: duplicate candle inserts after a `load_audit` failure are safe because serving reads deduped latest rows.
- Initial Load planning splits broad canonical `1m` / `1D` ranges into chunked `initial_load` jobs and stops enqueueing when queue backlog reaches the configured threshold.
- `systems/market-data/jobs/initial-load/main.py` provides a dry-run-first operational entrypoint for broad Initial Load planning and enqueueing.
- GapFill uses the configured NYSE-style market-calendar adapter with timezone, open/close, closed-date, and early-close env contracts.
- Backfill status TTL default is seven days for better AWS incident debugging.
- Compose and k8s config now explicitly set S&P 500 request config and Redis Streams backfill env.

## Verified

- `systems/market-data/tests/test_market_data_hardening.py` passes.
- `systems.api-server.tests.test_market_data_query` passes.
- Python compile check passes for market-data shared code, market-data jobs, and API app code.
- `git diff --check` passes.
- `npm run test:chart` and `npm run build` pass.
- Real local Redis smoke verified stream creation, claim metadata, and ack with an isolated key prefix.
- Local API smoke verified a `5m` backfill request queues a source `1m` job without synchronous S3/Alpaca execution.
- Browser smoke verified chart rendering, `1m` -> `5m` -> `1D` interval switching, Watch List, Hot Ranking, and symbol search with S&P 500 env applied.
- Fresh API/browser smoke verified `GET /api/charts/backfill/queue`, partial chart metadata, chart rendering, `5m` interval switching, Watch List, Hot Ranking, and Hot Ranking row selection updating the active chart.
- Manifest tests verify processed S3 manifest writes and manifest-first materialization.
- Raw manifest and replay tests verify raw S3 manifest lookup, processed-S3 replay, raw-S3 replay, correction replay from `updatedBars`, and Alpaca-only rejection for replay jobs.
- Queue metrics tests verify stream length, pending count, undelivered lag, backlog count, oldest pending request, and dead-letter count.
- Materializer retry tests verify the same candle row is reinserted safely after a ClickHouse audit failure and that the retry records `load_audit`.
- Initial Load planner tests verify bounded chunk creation, bulk priority, and backlog throttling.
- Initial Load job tests verify dry-run planning and enqueue behavior.
- Calendar adapter tests verify env-driven holidays and early closes suppress false GapFill ranges.
- GapFill tests verify adjacent-minute coalescing, weekend/closed-date exclusion, early-close handling, and ClickHouse timestamp-based internal missing range repair.

## Discovered And Fixed

- Browser smoke exposed that local compose/API execution could still inherit stale `.env` values such as `ALPACA_UNIVERSE=semiconductor-100`.
- Compose, k8s config, and env examples now explicitly pin:
  - `ALFAKA_REQUEST_CONFIG=systems/market-data/config/market-data-request.json`
  - `ALPACA_UNIVERSE=sp500`
  - `ALPACA_UNIVERSE_REGISTRY_PATH=systems/market-data/config/sp500-universe.json`

## Optional/Future Hardening

- Broader operational metrics for processor/Kafka/S3/ClickHouse freshness, retry/backpressure controls, and GapFill success/failure counts.
- Kafka replay is not required for Milestone 4 closure under the current v1 S3-first plan; revisit only if the user chooses processor-regeneration replay in v1.
- Optional hardening: replace the configured calendar adapter with a dependency-backed exchange calendar if dependency policy allows.
