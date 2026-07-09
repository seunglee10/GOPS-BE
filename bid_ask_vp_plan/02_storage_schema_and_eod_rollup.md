# 02 — Storage schema & EOD rollup

## 1. ClickHouse DDL — `market_data.order_flow_profile_daily` (FINAL)

```sql
CREATE TABLE IF NOT EXISTS market_data.order_flow_profile_daily (
    session_date        Date,
    symbol              LowCardinality(String),
    price_bin           Float64,
    price_bin_size      Float64,
    ask_volume          Float64,
    bid_volume          Float64,
    unknown_volume      Float64,
    ask_trade_count     UInt64,
    bid_trade_count     UInt64,
    unknown_trade_count UInt64,
    trade_count         UInt64,
    volume              Float64,
    classification_version LowCardinality(String) DEFAULT 'orderflow-estimated-v1',
    source              LowCardinality(String) DEFAULT 'clickhouse-rollup',
    feed                LowCardinality(String) DEFAULT 'sip',
    feed_profile        LowCardinality(String) DEFAULT feed,
    market_session      LowCardinality(String) DEFAULT 'regular',
    inserted_at         DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(inserted_at)
PARTITION BY toYYYYMM(session_date)
ORDER BY (symbol, session_date, price_bin_size, price_bin);
-- No TTL: permanent. delta and POC are derived at query time.
```

Numbers behind the sizing (why no TTL is fine): ~$0.01 bins over a typical large-cap daily range
produce O(10²–10³) rows per symbol-day; 5 symbols × 252 sessions × ~1,500 rows ≈ 2M rows/year —
negligible.

Rules:

- **Both DDL copies must be updated identically** (repo convention, and they have already drifted —
  do not "fix" unrelated drift in this change, just add the new table to both):
  1. `infra/clickhouse/initdb/01-market-data.sql`
  2. `infra/k8s/base/platform/clickhouse-initdb/01-market-data.sql`
  Append the `CREATE TABLE` at the end of the table section (before the ALTER migration block).
- If the codebase's `ensure_market_data_schema()` (see `alfaka/storage/clickhouse_loader.py`)
  maintains a programmatic table list, add this table there too so
  `CLICKHOUSE_ENSURE_SCHEMA_ON_START=true` environments (local) self-create it.
- AWS: initdb only runs on a fresh volume and `CLICKHOUSE_ENSURE_SCHEMA_ON_START` is `"false"` in
  AWS overlays, so this table must be applied to the live cluster **manually** as an additive DDL
  (see `06` §7 deploy notes). This is the established procedure per
  `docs/EKS_DATA_PRESERVING_REBUILD_PLAN.md` ("apply only needed additive DDL").

### 1.1 Query-time dedup convention

`ReplacingMergeTree` dedup is asynchronous. Every read of this table (REST service, agent context)
must query with `FROM market_data.order_flow_profile_daily FINAL` — acceptable because the table is
tiny and filtered by `symbol + session_date` range. Do not rely on background merges.

## 2. No durable 1m table in MVP

Intraday minute bins live only in the Redis HASH (`01` §2). If "recent-days durable intraday" is
wanted later, the reserved design is a `order_flow_bins_1m` table with
`TTL toDateTime(event_minute) + INTERVAL 2 DAY DELETE` — **do not build it now**; leave this note.

## 3. EOD rollup — `alfaka/orderflow/rollup.py` + job entrypoint

### 3.1 Job entrypoint (repo convention: thin wrapper)

`systems/market-data/jobs/order-flow-daily-rollup/main.py`:

```python
from alfaka.orderflow.rollup import main

if __name__ == "__main__":
    raise SystemExit(main())
```

CLI (argparse in `rollup.main`):

```text
--date YYYY-MM-DD        # session date (US/Eastern). Default: today's ET date if the regular
                         # session has ended, else the previous trading day.
--symbols NVDA,AMZN      # default: pinned_symbols_from_env()
--backfill-days N        # roll up the last N trading sessions ending at --date (inclusive)
--source ticks|alpaca    # default "ticks" (read trade_ticks/quote_ticks). "alpaca" fetches
                         # historical trades+quotes from Alpaca REST (for dates with no local ticks)
--dry-run                # compute + log metrics, skip INSERT
```

### 3.2 Core algorithm (per symbol, per session date) — `rollup_session(client, symbol, session_date, ...)`

1. **Window:** regular session bounds in UTC for `session_date`: 09:30–16:00 US/Eastern via
   `zoneinfo` (skip the date entirely if `market_session_for_datetime` says the date is closed).
   Quote warmup start = 09:25 ET.
2. **Streamed, hour-windowed reads** (bounds memory; NVDA quotes can be tens of millions of
   rows/day). For each hourly sub-window `[h, h+1h)` clamped to the session window:

   ```sql
   SELECT event_time, price, size, market_session
   FROM market_data.trade_ticks
   WHERE symbol = {symbol} AND event_time >= {win_start} AND event_time < {win_end}
   ORDER BY event_time ASC
   ```

   ```sql
   SELECT event_time, bid_price, ask_price, bid_size, ask_size
   FROM market_data.quote_ticks
   WHERE symbol = {symbol} AND event_time >= {win_start} AND event_time < {win_end}
   ORDER BY event_time ASC
   ```

   Use the existing `ClickHouseHttpClient` with `FORMAT JSONEachRow` and stream/iterate rows
   (check `alfaka/storage/clickhouse_loader.py` / `alfaka/serving/clickhouse_provider.py` for the
   HTTP client; add a row-streaming query helper if only whole-body queries exist).
   Select `source_event_id` and `feed_profile` in both queries (needed for dedup below).
3. **Dedup before classifying.** `trade_ticks`/`quote_ticks` are plain `MergeTree` fed by an
   at-least-once Kafka consumer — duplicate rows are possible and would inflate volumes. While
   streaming each ordered row stream, drop a row when its `source_event_id` was already seen
   (keep an LRU/deque-backed set of the last ~50k ids per stream; rows with NULL
   `source_event_id` pass through). Count drops into a `duplicateCount` metric. Also note:
   regular-session rows should be single-feed (`sip`) under the feed-session-window policy — do
   NOT hard-filter `feed_profile` (local dev may differ), but log the observed `feed_profile`
   distribution alongside the session metrics so cross-feed double-ingestion would be visible.
4. **Classify + accumulate:** feed both deduped streams (normalized to the camelCase trade/quote
   shape) into `merge_trades_with_quotes(trades, quotes, initial_quote=carry)`; keep `carry` =
   last quote of the window for the next hourly window. For each `(trade, quote)`:
   - skip `size <= 0`;
   - `side = classify_trade_side(trade, quote)`;
   - `price_bin = round(round(price / 0.01) * 0.01, 6)`; accumulate the same per-side fields as the
     live builder into a per-`price_bin` dict for the whole day (single day fits trivially in
     memory: O(10³) bins).
   - Track metrics: `trade_count`, `quote_count`, side distribution, `unknown_ratio`, and the
     distribution of `trade.market_session` values seen (this is the `market_session` verification
     signal — see `06` §5).
5. **Insert:** build one row per `price_bin` (schema §1, `feed`/`feed_profile` from the dominant
   feed seen in the window, `market_session='regular'`) and INSERT in one
   `INSERT ... FORMAT JSONEachRow` batch. Re-runs are safe: same `ORDER BY` key, newer
   `inserted_at` wins under `FINAL`.
6. **Audit/logging:** log a single structured summary line per symbol-day
   (`symbol, session_date, bins, tradeCount, quoteCount, duplicateCount, unknownRatio,
   marketSessionDistribution, feedProfileDistribution, durationMs`). If wiring into the existing `load_audit` table is straightforward (see how the
   clickhouse-loader writes it), also write one audit row; otherwise logging is sufficient for MVP.

Session filter policy: the **time window is the primary filter** (robust even if `market_session`
were mis-populated); the observed `market_session` distribution is logged and alarmed on (if
`regular` share of in-window trades < 99%, log at WARNING — do not fail the job).

### 3.3 `--source alpaca` (backfill for dates without local ticks)

- Check `alfaka/alpaca/` for an existing historical trades/quotes REST fetcher (the candle path has
  `fetch_alpaca_bars`). If none exists for trades/quotes, implement minimal paged fetchers against
  Alpaca Data API v2: `GET /v2/stocks/{symbol}/trades` and `GET /v2/stocks/{symbol}/quotes`
  (`start`, `end`, `limit=10000`, `page_token`, `feed=sip`), reusing the credential/session plumbing
  the existing Alpaca REST code uses. Stream pages directly into the same
  `merge_trades_with_quotes` accumulation — **never** buffer a full day of quotes in memory and do
  **not** write raw ticks anywhere; only the aggregate is stored.
- Rate limits: page sequentially, honor 429 with backoff (copy the existing Alpaca REST retry
  pattern if one exists).

### 3.4 Initial backfill decision (recommended, one-time)

After deploy, run the job once with `--backfill-days 10 --source alpaca` (falls back to `ticks`
automatically per date if local ticks fully cover the session — implement that check as: count
in-window `trade_ticks` rows; if > 0 use ticks, else alpaca). This seeds View A so it isn't empty at
launch. It is an operational step, not a code dependency — the feature works from day 1 without it.

## 4. Scheduling

### 4.1 Local (docker-compose)

Add a `jobs`-profile service `order-flow-daily-rollup` mirroring the existing `coverage-repair`
service pattern (same image as the market processor, command
`python -u systems/market-data/jobs/order-flow-daily-rollup/main.py`, env from the shared
market-data env block + `CLICKHOUSE_*`). Run manually:
`docker compose --profile jobs run --rm order-flow-daily-rollup`.

### 4.2 AWS — `infra/k8s/overlays/aws/cronjob-order-flow-daily-rollup.yaml`

Copy the structure of `cronjob-yahoo-estimates-sync.yaml` exactly (namespace
`alfaka-market-data`, `serviceAccountName: alfaka-market-data-sa`, nodepool `batch` +
`gops.io/dedicated=batch:NoSchedule` toleration, `envFrom: alfaka-market-data-config` +
clickhouse secret, `concurrencyPolicy: Forbid`, `ttlSecondsAfterFinished: 86400`,
`backoffLimit: 2`, `activeDeadlineSeconds: 3600`), with:

```yaml
# 21:30 UTC = 17:30 ET (EDT) / 16:30 ET (EST) — always after the 16:00 ET close, weekdays only
schedule: "30 21 * * 1-5"
containers[0]:
  image: YOUR_ECR_REPOSITORY/gops-market-processor:latest   # match how other market-data cronjobs pin images
  command: ["python", "-u", "systems/market-data/jobs/order-flow-daily-rollup/main.py"]
```

Register it in `infra/k8s/overlays/aws/kustomization.yaml` `resources:`. On closed dates the job
exits 0 quickly (the `market_session_for_datetime` closed-date check in §3.2 step 1).

## 5. Raw tick retention (explicitly OUT of scope)

`trade_ticks` / `quote_ticks` keep **no TTL in this rollout**. Pinned symbols will grow these tables
faster (quotes especially); a TTL (e.g. `event_time + INTERVAL 60 DAY`) is the obvious follow-up,
but it must be preceded by a dependency audit of every tick reader, and the user requires no
existing feature to break. Leave this paragraph as a `NOTE:` comment near the DDL in both initdb
files. Do not add the TTL.

## 6. Redis retention recap

Live order-flow HASH: `EXPIRE ORDER_FLOW_LIVE_TTL_SECONDS` (86400) refreshed on write + explicit
`DEL` on session-date rollover (`01` §3.1). Nothing else to clean.

## 7. Tests for this file's scope

- `systems/market-data/tests/test_orderflow_rollup.py`:
  - synthetic trades+quotes (reuse the scenario data from the old `test_footprint.py`) through
    `rollup_session` against a fake ClickHouse client → exact expected rows (bins, side volumes,
    counts, `market_session='regular'`);
  - hourly-window quote carry: a quote at 09:59:59 must classify a 10:00:01 trade;
  - duplicate `source_event_id` rows are counted and excluded from volumes;
  - closed date → no rows, exit ok;
  - `--dry-run` inserts nothing;
  - re-run produces identical rows (same key, newer `inserted_at`).
- DDL presence test: extend the hardening test pattern (`test_market_data_hardening.py` greps
  initdb SQL) to assert `order_flow_profile_daily` exists in **both** initdb copies with the exact
  `ORDER BY (symbol, session_date, price_bin_size, price_bin)`.
