-- 역할: AI 기업저널의 검증된 결과와 비동기 생성 상태를 ClickHouse에 보존합니다.
-- 원칙: 기존 뉴스·가격·재무 원문은 복제하지 않고 source id와 서버 계산 결과만 저장합니다.

CREATE DATABASE IF NOT EXISTS market_data;

CREATE TABLE IF NOT EXISTS market_data.company_journal_reports_v1
(
    symbol LowCardinality(String),
    instrument_id Nullable(UUID),
    schema_version LowCardinality(String) DEFAULT 'company-journal.v2',
    analysis_as_of Date,
    generated_at DateTime64(3, 'UTC'),
    input_digest String,
    contract_version LowCardinality(String),
    headline String,
    keywords Array(String),
    recent_movement String,
    financial_stability String,
    watch_items String,
    tab_narratives_json String,
    server_metrics_json String,
    news_ids Array(String),
    sec_filing_ids Array(String),
    price_as_of Nullable(Date),
    graph_relation_ids Array(String),
    missing_data Array(String),
    validation_status LowCardinality(String),
    validation_errors Array(String),
    model LowCardinality(String),
    prompt_version LowCardinality(String),
    source_receipt_json String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(analysis_as_of)
ORDER BY (symbol, analysis_as_of, input_digest, generated_at);

CREATE TABLE IF NOT EXISTS market_data.company_journal_generation_events_v1
(
    request_id String,
    symbol LowCardinality(String),
    instrument_id Nullable(UUID),
    schema_version LowCardinality(String) DEFAULT 'company-journal.v2',
    analysis_as_of Date,
    input_digest String,
    status LowCardinality(String),
    requested_source LowCardinality(String),
    error Nullable(String),
    occurred_at DateTime64(3, 'UTC'),
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(analysis_as_of)
ORDER BY (request_id, occurred_at);

CREATE OR REPLACE VIEW market_data.company_journal_reports AS
SELECT * FROM market_data.company_journal_reports_v1;

CREATE OR REPLACE VIEW market_data.company_journal_generation_events AS
SELECT * FROM market_data.company_journal_generation_events_v1;
