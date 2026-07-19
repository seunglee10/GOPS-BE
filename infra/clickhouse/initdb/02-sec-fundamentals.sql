-- 역할: 로컬 ClickHouse에 SEC fundamentals 조회용 기본 테이블을 만듭니다.
-- 사용: Docker Compose가 ClickHouse를 처음 띄울 때 자동 실행됩니다.
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

CREATE TABLE IF NOT EXISTS market_data.yahoo_analyst_summaries
(
    symbol LowCardinality(String),
    statement String,
    tone LowCardinality(String),
    source_as_of Nullable(DateTime64(3, 'UTC')),
    replay_statement String DEFAULT '',
    replay_tone LowCardinality(String) DEFAULT 'neutral',
    replay_source_as_of Nullable(DateTime64(3, 'UTC')),
    replay_cutoff Nullable(DateTime64(3, 'UTC')),
    source LowCardinality(String),
    collected_at DateTime64(3, 'UTC'),
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(collected_at)
ORDER BY symbol
TTL toDateTime(collected_at) + INTERVAL 1 DAY DELETE;

-- Existing environments keep this file idempotent and receive the additive event fields.
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS event_at Nullable(DateTime64(3, 'UTC')) AFTER analyst_count;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS actual_value Nullable(Float64) AFTER event_at;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS surprise_percent Nullable(Float64) AFTER actual_value;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS event_session LowCardinality(String) DEFAULT 'unknown' AFTER surprise_percent;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS event_status LowCardinality(String) DEFAULT 'scheduled' AFTER event_session;
ALTER TABLE market_data.yahoo_analyst_summaries ADD COLUMN IF NOT EXISTS replay_statement String DEFAULT '' AFTER source_as_of;
ALTER TABLE market_data.yahoo_analyst_summaries ADD COLUMN IF NOT EXISTS replay_tone LowCardinality(String) DEFAULT 'neutral' AFTER replay_statement;
ALTER TABLE market_data.yahoo_analyst_summaries ADD COLUMN IF NOT EXISTS replay_source_as_of Nullable(DateTime64(3, 'UTC')) AFTER replay_tone;
ALTER TABLE market_data.yahoo_analyst_summaries ADD COLUMN IF NOT EXISTS replay_cutoff Nullable(DateTime64(3, 'UTC')) AFTER replay_source_as_of;
