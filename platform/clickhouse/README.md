# ClickHouse Platform Contract

ClickHouse is the confirmed historical serving store for chart data.
Redis keeps only latest 120 candles and live state; S3 final/manifest is the
durable rebuild source.

## Current Chart Tables

```text
market_data.chart_candles
market_data.trade_ticks
market_data.quote_ticks
market_data.market_events
market_data.market_status_events
market_data.volume_profile_bins_1m
market_data.backfill_jobs
market_data.storage_object_audit
market_data.load_audit
```

## SEC Fundamentals Tables

Financial Agent runtime reads the normalized SEC serving projection below. The
runtime must not call SEC APIs on user requests.

DDL is maintained in both local and EKS init paths:

```text
infra/clickhouse/initdb/02-sec-fundamentals.sql
infra/k8s/base/platform/clickhouse-initdb/02-sec-fundamentals.sql
```

```text
market_data.sec_company_tickers
market_data.sec_filing_events
market_data.sec_raw_artifacts
market_data.sec_financial_facts
market_data.sec_derived_metrics
market_data.sec_frames
market_data.sec_collection_runs
```

`market_data.sec_company_tickers` stores ticker/CIK mapping and
`is_active_universe_member`. S&P 500 membership comes from
`systems/market-data/config/sp500-universe.json`; when a company leaves the
universe, existing facts and metrics remain and only membership is updated.

`market_data.sec_financial_facts` stores normalized source facts.
`market_data.sec_derived_metrics` stores deterministic metrics such as margins,
YoY growth, liabilities ratios, total debt ratios, current ratio, FCF, and
interest coverage. `market_data.sec_frames` stores SEC frame rows as originally
reported, so frame values may differ from later corrected companyfacts rows.

Recommended engine for fact/metric projections is a replacing table keyed by
symbol, metric/concept, unit, fiscal period, period end, accession or synthetic
source hash, with `version_filed_at` as the revision version.

## Excluded From The Rebuild Contract

```text
market_data.market_quotes
```

Quote layer payloads are persisted to `market_data.quote_ticks`. Raw S3 backup
objects must not be loaded into ClickHouse by the normal chart path.
