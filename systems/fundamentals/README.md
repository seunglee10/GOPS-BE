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

- Default source (`SEC_FUNDAMENTALS_SOURCE=api`): per-company
  `data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json` calls for the universe
  (~500 companies), plus bounded SEC frames API calls for peer-comparison
  frames. Each API response is the company's full XBRL history, so no separate
  initial load is needed. Raw JSON is overwritten in S3 at a stable per-CIK key
  (`{prefix}/api/CIK{cik}.json`), so S3 does not accumulate daily snapshots.
- Legacy source (`SEC_FUNDAMENTALS_SOURCE=zip`): SEC `companyfacts.zip` bulk
  download (multi-GB, all SEC filers). Automatically selected when
  `SEC_COMPANYFACTS_ZIP_PATH` or `SEC_COMPANYFACTS_S3_KEY` is set. Prefer this
  only if the universe grows to thousands of companies.
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

- fetches per-company SEC companyfacts JSON (API mode, default) or downloads
  SEC `companyfacts.zip` (zip mode);
- uploads raw JSON per CIK (API mode, overwriting stable keys) or the raw ZIP
  (zip mode, date-keyed) to S3 under `SEC_FUNDAMENTALS_S3_PREFIX`;
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

In AWS/EKS, scheduled refresh is handled by
`infra/k8s/overlays/aws/scheduled/cronjob-sec-fundamentals-sync.yaml`. The CronJob reads
`SEC_USER_AGENT` from Kubernetes Secret `alfaka-sec-fundamentals-secret`, which
is synced from AWS Secrets Manager path
`/gops/prod/fundamentals/sec-user-agent` property `SEC_USER_AGENT` by
`infra/k8s/overlays/aws/scheduled/externalsecret-sec-fundamentals.yaml`. Once those
resources are applied, the daily SEC refresh runs in EKS without a local laptop
session.

Manual AWS execution can also use the Kubernetes Secret reference:

```sh
SEC_FUNDAMENTALS_DRY_RUN=false \
./scripts/aws/run-sec-fundamentals-backfill-job.sh
```

Use `SEC_FUNDAMENTALS_SYMBOLS=AAPL,NVDA` or
`SEC_FUNDAMENTALS_MAX_COMPANIES=10` for limited test loads.
Use `SEC_FUNDAMENTALS_SOURCE=zip` to force the legacy bulk-ZIP path.
Use `SEC_COMPANYFACTS_S3_KEY=fundamentals/sec/companyfacts/YYYY-MM-DD/companyfacts.zip`
to reuse an already archived SEC bulk ZIP during chunked replays (implies zip
mode).
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
yahoo_earnings_estimates
```

Redis stale checks are not part of the Financial Agent runtime. Sync or nightly
reconcile jobs compare ClickHouse revisions and rewrite summary/peer keys.

## Heatmap Consumers

The market heatmap does not collect SEC data. It reads this system's Redis
summary key first and falls back to ClickHouse `sec_financial_facts` plus
`sec_company_tickers`. Keep the `shares_outstanding` metric populated in either
store so `/api/market/heatmap?universe=sp500` can compute
`marketCap = lastPrice * sharesOutstanding`.

Actual financial statement metrics and forecast metrics are separate. SEC EDGAR
actuals power company information and investment ratios. Yahoo/yfinance EPS and
revenue estimates are stored in `market_data.yahoo_earnings_estimates` by a
separate daily collector and are only consumed as forecast/consensus overlays.

## Yahoo Estimates Sync

The Yahoo estimates entrypoint is:

```sh
python -u systems/fundamentals/jobs/yahoo-estimates-sync/main.py
```

It runs in the `gops-market-storage` image and writes only
`market_data.yahoo_earnings_estimates`. It does not write SEC actual tables or
Redis fundamentals summaries. In AWS/EKS the scheduled collector is
`infra/k8s/overlays/aws/scheduled/cronjob-yahoo-estimates-sync.yaml`.

Default execution is a dry-run:

```sh
YAHOO_ESTIMATES_DRY_RUN=true \
python -u systems/fundamentals/jobs/yahoo-estimates-sync/main.py
```

Actual AWS execution uses:

```sh
YAHOO_ESTIMATES_DRY_RUN=false \
python -u systems/fundamentals/jobs/yahoo-estimates-sync/main.py
```

Rows are keyed in ClickHouse by
`symbol + metric + fiscal_year + fiscal_period + period_end` with
`ReplacingMergeTree(collected_at)`, so daily refreshes replace the latest
consensus for the same period instead of accumulating duplicate history.
