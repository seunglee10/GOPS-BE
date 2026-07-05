# Fundamentals System

SEC fundamentals are collected outside the agent hot path. Runtime agents read
Redis/ClickHouse snapshots only; they must not call SEC APIs on user requests.

## Runtime Contracts

- S&P 500 source of truth: `systems/market-data/config/sp500-universe.json`
- ClickHouse database: `market_data`
- Redis summary key: `gops:fundamentals:summary:v1:{SYMBOL}`
- Redis peer keys:
  - `gops:fundamentals:peer:v1:{SYMBOL}:latest`
  - `gops:fundamentals:peer:v1:{SYMBOL}:{FRAME_PERIOD}`

`systems/fundamentals/shared/fundamentals` owns deterministic SEC concept
mapping and derived metric calculations. OpenAI is not used for numeric
normalization, missing-value repair, or metric calculation.

## Collection Shape

- Initial load: SEC `companyfacts.zip` bulk data for company facts plus
  bounded SEC frames API calls for peer-comparison frames.
- Incremental detection: EDGAR latest filings RSS/full-index or submissions
  index.
- Recompute trigger: new `10-K`, `10-Q`, `10-K/A`, or `10-Q/A`.
- `8-K`: store as `sec_filing_events` only; do not recompute derived metrics by
  default.
- SEC HTTP calls require `SEC_USER_AGENT` with contact info and an 8 req/s
  limiter.

## Implemented Backfill Job

The implemented initial-load entrypoint is:

```sh
python -u systems/fundamentals/jobs/sec-companyfacts-backfill/main.py
```

It runs in the `gops-market-storage` image so it can reuse the existing S3,
ClickHouse, and Redis helpers. The job:

- downloads SEC `companyfacts.zip`;
- uploads the raw ZIP to S3 under `SEC_FUNDAMENTALS_S3_PREFIX`;
- normalizes selected S&P 500 company facts into `market_data.sec_financial_facts`;
- computes deterministic derived metrics into `market_data.sec_derived_metrics`;
- fetches bounded SEC frames API periods into `market_data.sec_frames`;
- writes `market_data.sec_company_tickers`, `market_data.sec_raw_artifacts`, and
  `market_data.sec_collection_runs`;
- writes Redis summary and peer cache keys when data is available.

Default execution is a dry-run and skips the network download:

```sh
SEC_FUNDAMENTALS_DRY_RUN=true \
python -u systems/fundamentals/jobs/sec-companyfacts-backfill/main.py
```

Actual AWS execution should use the wrapper:

```sh
SEC_FUNDAMENTALS_DRY_RUN=false \
SEC_USER_AGENT="GOPS fundamentals contact@example.com" \
./scripts/aws/run-sec-fundamentals-backfill-job.sh
```

Use `SEC_FUNDAMENTALS_SYMBOLS=AAPL,NVDA` or
`SEC_FUNDAMENTALS_MAX_COMPANIES=10` for limited test loads.
Use `SEC_COMPANYFACTS_S3_KEY=fundamentals/sec/companyfacts/YYYY-MM-DD/companyfacts.zip`
to reuse an already archived SEC bulk ZIP during chunked replays.
Use `SEC_FUNDAMENTALS_FRAME_PERIODS=CY2026Q1,CY2025Q4` to override the
default recent comparable frame periods.
Use `SEC_FUNDAMENTALS_LOAD_COMPANYFACTS=false` with
`SEC_FUNDAMENTALS_LOAD_FRAMES=true` for frames-only refreshes.
Use `SEC_FUNDAMENTALS_WRITE_FRAME_ROWS=false` when `sec_frames` is already
loaded and only Redis peer cache should be rewritten.

## Storage Shape

ClickHouse tables live under `market_data`:

```text
sec_company_tickers
sec_filing_events
sec_raw_artifacts
sec_financial_facts
sec_derived_metrics
sec_frames
sec_collection_runs
```

Redis stale checks are not part of the Financial Agent runtime. Sync or nightly
reconcile jobs compare ClickHouse revisions and rewrite summary/peer keys.
