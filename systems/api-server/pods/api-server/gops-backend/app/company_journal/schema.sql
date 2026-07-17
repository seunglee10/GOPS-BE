CREATE TABLE IF NOT EXISTS company_journal_reports_v1
(
    symbol LowCardinality(String), analysis_as_of Date, generated_at DateTime64(3, 'UTC'),
    input_digest String, contract_version LowCardinality(String), headline String, keywords Array(String),
    recent_movement String, financial_stability String, watch_items String, tab_narratives_json String,
    server_metrics_json String, news_ids Array(String), sec_filing_ids Array(String), price_as_of Nullable(Date),
    graph_relation_ids Array(String), missing_data Array(String), validation_status LowCardinality(String),
    validation_errors Array(String), model LowCardinality(String), prompt_version LowCardinality(String),
    source_receipt_json String, inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(analysis_as_of)
ORDER BY (symbol, analysis_as_of, input_digest, generated_at);

CREATE TABLE IF NOT EXISTS company_journal_generation_events_v1
(
    request_id String, symbol LowCardinality(String), analysis_as_of Date, input_digest String,
    status LowCardinality(String), requested_source LowCardinality(String), error Nullable(String),
    occurred_at DateTime64(3, 'UTC'), inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(analysis_as_of)
ORDER BY (request_id, occurred_at);
