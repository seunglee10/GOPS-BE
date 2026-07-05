from __future__ import annotations


CLICKHOUSE_TABLES = {
    "sec_company_tickers": """
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
ORDER BY symbol
""",
    "sec_filing_events": """
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
ORDER BY (symbol, form, filed_at, accession)
""",
    "sec_raw_artifacts": """
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
ORDER BY (symbol, artifact_type, object_path)
""",
    "sec_financial_facts": """
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
ORDER BY (symbol, metric, unit, fiscal_year, fiscal_period, period_end)
""",
    "sec_derived_metrics": """
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
ORDER BY (symbol, metric, fiscal_year, fiscal_period, period_end)
""",
    "sec_frames": """
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
ORDER BY (frame_period, taxonomy, concept, unit, symbol)
""",
    "sec_collection_runs": """
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
ORDER BY (job_type, run_id)
""",
}
