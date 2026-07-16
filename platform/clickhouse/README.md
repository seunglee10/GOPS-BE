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

`trade_ticks` and `quote_ticks` retain 21 days and keep the newest 100,000
non-replicated insert-deduplication tokens per table. The tick loader derives a
token from Kafka topic/partition/offset metadata, commits only offsets included
in a successful insert, and keeps a bounded recent `sourceEventId` cache for
short replays that cross insert batch boundaries. `chart_candles` and
`order_flow_profile_daily` have no deletion TTL. The compatibility
`chart_analysis_assets` table also has no TTL while the PostgreSQL latest-row
migration is in progress.
`chart_analysis_assets` uses `ReplacingMergeTree(inserted_at)` ordered by
`(symbol, interval)`; readers use `FINAL` or `argMax` so each pair serves only
the latest prebuilt asset. The current single-replica builder serializes
`generatedAt + canonical payload digest` compare-and-insert so a delayed older
build is suppressed; dual modes also warn if a monotonic no-op leaves the two
stores divergent. Existing environments apply the tick TTL and deduplication
window through the operator-reviewed, idempotent migration:

```text
scripts/local/migrate-chart-tick-retention.sql
```

`chart_candles.bucket_policy` separates incompatible intraday bucket identities.
Legacy/native clock rows use `clock_aligned`; new US-equity derived rows use
`us_equity_regular_session`. Readers select only the latter for
`5m/10m/1h/4h`. Realtime source `1m`, historical hourly-repair source `10m`,
and session-derived rows are persisted. The `10m` recovery rows use
`source_native`; target `1h/4h` rows use `us_equity_regular_session`. Hourly
read fallback is stored target, then `10m` aggregation, then legacy `1m`
aggregation, so chart serving, Geometry, and SMA share the same OHLCV facts.
While a pre, after, or overnight session is active, readers also query only the
bounded current and contiguous prior session's `1m` rows and expose a
session-anchored `us_equity_extended_session` aggregate. This aggregate is a
serving result, not an additional historical `chart_candles` persistence path;
old extended sessions remain excluded.
`bucket_policy_key` mirrors that value only for the ReplacingMergeTree sorting
key. Writers set both fields explicitly; the key intentionally has no default
expression because ClickHouse forbids adding such a column to an existing sorting
key. Adding it in the same migration statement as the sorting-key change lets an
existing table adopt the new identity without deleting legacy rows.

The operator migration and one-year rebuild entrypoint is:

```bash
APPLY=true WAIT_FOR_JOB=false scripts/aws/run-session-candle-rebuild-job.sh
```

The script adds the column idempotently before starting the rebuild Job. It never
deletes legacy rows; readers exclude them by policy and an operator may clean them
only after validation and the rollback window.

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

`market_data.chart_analysis_assets` stores compact final v1 or v2 JSON payloads
as the default and rollback source until the guarded PostgreSQL cutover finishes.
The v2 rollout reuses the existing `asset_version` column and table: there is no
new table, TTL, or candidate ledger. A builder insert is skipped when the final
`assetContentDigest` is unchanged; raw candles, rejected candidates, prompts,
and provider responses are never persisted here. Latest reads continue to use
`argMax(payload, inserted_at)` during mixed v1/v2 rollout.
In `dual_clickhouse_read` and `dual_postgres_read`, writes are mirrored while
only one store serves reads. Canonical candles and request-scoped repair
materialization always stay in ClickHouse; only the latest final asset JSON moves.
The authenticated development route can explicitly delete selected
`(symbol, interval)` histories with a synchronous mutation. This exists for
iteration and recovery only; it does not add a TTL or background cleanup.

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
market_data.yahoo_earnings_estimates
```

`market_data.sec_company_tickers` stores ticker/CIK mapping and
`is_active_universe_member`. S&P 500 membership comes from
`systems/market-data/config/sp500-universe.json`; when a company leaves the
universe, existing facts and metrics remain and only membership is updated.

`market_data.yahoo_earnings_estimates` also stores chart-event fields
`event_at`, `actual_value`, `surprise_percent`, `event_session`, and
`event_status`. Existing volumes apply these additions through the idempotent
operator script below; both local and EKS DDL copies include the same columns.

```text
scripts/local/migrate-yahoo-earnings-events.sql
```

The chart events reader selects the latest `collected_at` revision for each
stored event. It reads `news_company_daily_summaries.raw.sources` for at most
three article links and does not call an external provider on the request path.

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
