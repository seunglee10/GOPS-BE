# ClickHouse Platform Contract

ClickHouse is the confirmed historical serving store for chart data.
Redis keeps only latest 120 candles and live state; S3 final/manifest is
durable recovery evidence.

## Current Chart Tables

```text
market_data.chart_candles
market_data.trade_ticks
market_data.quote_ticks
market_data.market_events
market_data.market_status_events
market_data.order_flow_profile_daily
market_data.chart_analysis_assets
market_data.backfill_jobs
market_data.storage_object_audit
market_data.load_audit
```

`trade_ticks` and `quote_ticks` retain 21 days. `chart_candles` and
`order_flow_profile_daily` and `chart_analysis_assets` have no deletion TTL.
`chart_analysis_assets` uses `ReplacingMergeTree(inserted_at)` ordered by
`(symbol, interval)`; readers use `FINAL` or `argMax` so each pair serves only
the latest prebuilt asset. Existing environments apply
the TTL through the operator-reviewed, idempotent migration:

```text
scripts/local/migrate-chart-tick-retention.sql
```

Optional indicators and candle volume profile are calculated by the API and
cached in Redis; ClickHouse does not store request-hash artifacts. The retired
tick-volume-profile and derived-artifact tables are not created in fresh
environments. Existing tables are not dropped automatically.

`market_data.order_flow_profile_daily` is the daily bid/ask order-flow table.
DDL is maintained in both local and EKS init paths:

```text
infra/clickhouse/initdb/01-market-data.sql
infra/k8s/base/platform/clickhouse-initdb/01-market-data.sql
```

`scripts/local/check-chart-data-contracts.py` enforces normalized equality of
the two market-data DDL copies. Environment headers and the declared local-only
agent table are the only allowed difference.

## Chart Analysis Assets

`market_data.chart_analysis_assets` stores compact final v1 or v2 JSON payloads.
The v2 rollout reuses the existing `asset_version` column and table: there is no
new table, TTL, or candidate ledger. A builder insert is skipped when the final
`assetContentDigest` is unchanged; raw candles, rejected candidates, prompts,
and provider responses are never persisted here. Latest reads continue to use
`argMax(payload, inserted_at)` during mixed v1/v2 rollout.

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

## Excluded From The Chart Contract

```text
market_data.market_quotes
```

Quote layer payloads are persisted to `market_data.quote_ticks`. Raw S3 backup
objects must not be loaded into ClickHouse by the normal chart path.
