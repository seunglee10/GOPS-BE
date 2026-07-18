-- 역할: EKS ClickHouse에 SEC fundamentals 조회용 기본 테이블을 만듭니다.
-- 주의: agent runtime은 이 테이블과 Redis fundamentals cache만 읽고, 사용자 요청 중 SEC API를 호출하지 않습니다.

CREATE DATABASE IF NOT EXISTS market_data;

CREATE TABLE IF NOT EXISTS market_data.sec_company_tickers
(
    symbol LowCardinality(String),
    cik String,
    company_name String,
    exchange LowCardinality(String),
    is_active_universe_member UInt8,
    universe_source String,
    updated_at DateTime64(3, 'UTC'),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY symbol;

CREATE TABLE IF NOT EXISTS market_data.sec_filing_events
(
    symbol LowCardinality(String),
    cik String,
    form LowCardinality(String),
    filed_at Date,
    accession String,
    items Array(String),
    event_only UInt8,
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)
ORDER BY (symbol, form, filed_at, accession);

CREATE TABLE IF NOT EXISTS market_data.sec_raw_artifacts
(
    symbol LowCardinality(String),
    cik String,
    artifact_type LowCardinality(String),
    object_path String,
    checksum String,
    source_url String,
    collected_at DateTime64(3, 'UTC'),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(collected_at)
ORDER BY (symbol, artifact_type, object_path);

CREATE TABLE IF NOT EXISTS market_data.sec_financial_facts
(
    symbol LowCardinality(String),
    cik String,
    metric LowCardinality(String),
    taxonomy LowCardinality(String),
    concept String,
    unit LowCardinality(String),
    value Nullable(Float64),
    fiscal_year UInt16,
    fiscal_period LowCardinality(String),
    period_end Date,
    form LowCardinality(String),
    accession Nullable(String),
    filed_at Date,
    quality LowCardinality(String),
    raw String,
    version_filed_at Date,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(version_filed_at)
ORDER BY (symbol, metric, unit, fiscal_year, fiscal_period, period_end);

CREATE TABLE IF NOT EXISTS market_data.sec_derived_metrics
(
    symbol LowCardinality(String),
    cik String,
    metric LowCardinality(String),
    value Nullable(Float64),
    fiscal_year UInt16,
    fiscal_period LowCardinality(String),
    period_end Date,
    form LowCardinality(String),
    accession Nullable(String),
    filed_at Date,
    quality LowCardinality(String),
    raw String,
    version_filed_at Date,
    computed_at DateTime64(3, 'UTC'),
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(version_filed_at)
ORDER BY (symbol, metric, fiscal_year, fiscal_period, period_end);

CREATE TABLE IF NOT EXISTS market_data.sec_frames
(
    frame_period LowCardinality(String),
    taxonomy LowCardinality(String),
    concept String,
    unit LowCardinality(String),
    symbol LowCardinality(String),
    cik String,
    value Nullable(Float64),
    accession String,
    filed_at Date,
    quality LowCardinality(String),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)
ORDER BY (frame_period, taxonomy, concept, unit, symbol);

CREATE TABLE IF NOT EXISTS market_data.sec_collection_runs
(
    run_id String,
    job_type LowCardinality(String),
    status LowCardinality(String),
    symbol_count UInt32,
    started_at DateTime64(3, 'UTC'),
    finished_at Nullable(DateTime64(3, 'UTC')),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)
ORDER BY (job_type, run_id);

CREATE TABLE IF NOT EXISTS market_data.yahoo_earnings_estimates
(
    symbol LowCardinality(String),
    metric LowCardinality(String),
    fiscal_year UInt16,
    fiscal_period LowCardinality(String),
    period_end Date,
    average Nullable(Float64),
    low Nullable(Float64),
    high Nullable(Float64),
    analyst_count Nullable(UInt16),
    event_at Nullable(DateTime64(3, 'UTC')),
    actual_value Nullable(Float64),
    surprise_percent Nullable(Float64),
    event_session LowCardinality(String) DEFAULT 'unknown',
    event_status LowCardinality(String) DEFAULT 'scheduled',
    source LowCardinality(String),
    collected_at DateTime64(3, 'UTC'),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(collected_at)
ORDER BY (symbol, metric, fiscal_year, fiscal_period, period_end);

CREATE TABLE IF NOT EXISTS market_data.yahoo_analyst_actions
(
    symbol LowCardinality(String),
    action_at DateTime64(3, 'UTC'),
    firm String,
    action LowCardinality(String),
    from_grade String,
    to_grade String,
    prior_price_target Nullable(Float64),
    price_target Nullable(Float64),
    source LowCardinality(String),
    collected_at DateTime64(3, 'UTC'),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(collected_at)
ORDER BY (symbol, action_at, firm, action);

CREATE TABLE IF NOT EXISTS market_data.yahoo_analyst_consensus
(
    symbol LowCardinality(String),
    snapshot_date Date,
    current_price Nullable(Float64),
    target_low Nullable(Float64),
    target_high Nullable(Float64),
    target_mean Nullable(Float64),
    target_median Nullable(Float64),
    strong_buy Nullable(UInt16),
    buy Nullable(UInt16),
    hold Nullable(UInt16),
    sell Nullable(UInt16),
    strong_sell Nullable(UInt16),
    source LowCardinality(String),
    collected_at DateTime64(3, 'UTC'),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(collected_at)
ORDER BY (symbol, snapshot_date);

-- Existing environments keep this file idempotent and receive the additive event fields.
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS event_at Nullable(DateTime64(3, 'UTC')) AFTER analyst_count;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS actual_value Nullable(Float64) AFTER event_at;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS surprise_percent Nullable(Float64) AFTER actual_value;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS event_session LowCardinality(String) DEFAULT 'unknown' AFTER surprise_percent;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS event_status LowCardinality(String) DEFAULT 'scheduled' AFTER event_session;
