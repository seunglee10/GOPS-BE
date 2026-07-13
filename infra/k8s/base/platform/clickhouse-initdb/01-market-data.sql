-- 역할: ClickHouse에 시장 데이터 조회용 기본 테이블을 만듭니다.
-- 사용: in-cluster ClickHouse dev/test 배포가 처음 뜰 때 자동 실행됩니다.
-- 주의: 운영 적재 방식은 S3/Parquet sink 또는 Python realtime processor로 교체/확장할 수 있습니다.

CREATE DATABASE IF NOT EXISTS market_data;

CREATE TABLE IF NOT EXISTS market_data.trade_ticks
(
    event_time DateTime64(3, 'UTC'),
    symbol LowCardinality(String),
    trade_id UInt64 DEFAULT 0,
    price Float64,
    size Nullable(Float64),
    exchange Nullable(String),
    conditions Array(String),
    tape Nullable(String),
    source LowCardinality(String),
    feed LowCardinality(String),
    feed_profile LowCardinality(String) DEFAULT feed,
    market_session LowCardinality(String) DEFAULT 'unknown',
    source_event_id Nullable(String),
    received_at Nullable(DateTime64(3, 'UTC')),
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_time)
ORDER BY (symbol, event_time, feed_profile, trade_id)
TTL toDateTime(event_time) + INTERVAL 21 DAY DELETE
SETTINGS non_replicated_deduplication_window = 100000;

CREATE TABLE IF NOT EXISTS market_data.quote_ticks
(
    event_time DateTime64(3, 'UTC'),
    symbol LowCardinality(String),
    bid_price Nullable(Float64),
    bid_size Nullable(Float64),
    ask_price Nullable(Float64),
    ask_size Nullable(Float64),
    bid_exchange Nullable(String),
    ask_exchange Nullable(String),
    conditions Array(String),
    source LowCardinality(String),
    feed LowCardinality(String),
    feed_profile LowCardinality(String) DEFAULT feed,
    market_session LowCardinality(String) DEFAULT 'unknown',
    source_event_id Nullable(String),
    received_at Nullable(DateTime64(3, 'UTC')),
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_time)
ORDER BY (symbol, event_time, feed_profile)
TTL toDateTime(event_time) + INTERVAL 21 DAY DELETE
SETTINGS non_replicated_deduplication_window = 100000;

CREATE TABLE IF NOT EXISTS market_data.chart_candles
(
    event_time DateTime64(3, 'UTC'),
    symbol LowCardinality(String),
    interval LowCardinality(String),
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    volume Float64,
    trade_count Nullable(UInt64),
    vwap Nullable(Float64),
    ma5 Nullable(Float64),
    ma20 Nullable(Float64),
    ma60 Nullable(Float64),
    is_closed Bool,
    correction_type LowCardinality(String),
    source LowCardinality(String),
    feed LowCardinality(String),
    feed_profile LowCardinality(String) DEFAULT feed,
    market_session LowCardinality(String) DEFAULT 'unknown',
    price_adjustment LowCardinality(String) DEFAULT 'unknown',
    canonical_version LowCardinality(String) DEFAULT 'legacy',
    bucket_policy LowCardinality(String) DEFAULT 'clock_aligned',
    bucket_policy_key LowCardinality(String),
    source_event_id Nullable(String),
    created_at Nullable(DateTime64(3, 'UTC')),
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)
PARTITION BY toYYYYMM(event_time)
ORDER BY (symbol, interval, event_time, feed_profile, market_session, bucket_policy_key);

CREATE TABLE IF NOT EXISTS market_data.market_status_events
(
    event_time DateTime64(3, 'UTC'),
    symbol Nullable(String),
    status_type LowCardinality(String),
    status String,
    reason Nullable(String),
    source LowCardinality(String),
    feed LowCardinality(String),
    feed_profile LowCardinality(String) DEFAULT feed,
    market_session LowCardinality(String) DEFAULT 'unknown',
    source_event_id Nullable(String),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)
PARTITION BY toYYYYMM(event_time)
ORDER BY (coalesce(symbol, '_MARKET'), status_type, event_time, feed_profile, market_session);

CREATE TABLE IF NOT EXISTS market_data.market_events
(
    event_time DateTime64(3, 'UTC'),
    symbol Nullable(String),
    event_type LowCardinality(String),
    layer LowCardinality(String),
    source LowCardinality(String),
    feed LowCardinality(String),
    feed_profile LowCardinality(String) DEFAULT feed,
    market_session LowCardinality(String) DEFAULT 'unknown',
    source_event_id Nullable(String),
    payload String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)
PARTITION BY toYYYYMM(event_time)
ORDER BY (coalesce(symbol, '_MARKET'), event_type, event_time, feed_profile, market_session);

CREATE TABLE IF NOT EXISTS market_data.symbols
(
    symbol String,
    name String,
    exchange Nullable(String),
    market LowCardinality(String),
    asset_class LowCardinality(String),
    tradable Bool,
    status LowCardinality(String),
    source LowCardinality(String),
    updated_at DateTime64(3, 'UTC'),
    raw Nullable(String),
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)
ORDER BY symbol;

CREATE TABLE IF NOT EXISTS market_data.news_articles
(
    published_at DateTime64(3, 'UTC'),
    symbol LowCardinality(String),
    article_id String,
    headline String,
    summary Nullable(String),
    content Nullable(String),
    url Nullable(String),
    source Nullable(String),
    author Nullable(String),
    updated_at Nullable(DateTime64(3, 'UTC')),
    received_at Nullable(DateTime64(3, 'UTC')),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)
PARTITION BY toYYYYMM(published_at)
ORDER BY (symbol, published_at, article_id)
TTL toDateTime(published_at) + INTERVAL 30 DAY DELETE;

CREATE TABLE IF NOT EXISTS market_data.news_article_localizations
(
    published_at DateTime64(3, 'UTC'),
    symbol LowCardinality(String),
    article_id String,
    locale LowCardinality(String),
    symbols Array(String),
    target_symbol LowCardinality(String),
    subject_relevance LowCardinality(String),
    relevance_score_v2 Float32,
    relevance_reason String,
    direct_signals Array(String),
    headline Nullable(String),
    summary Nullable(String),
    url Nullable(String),
    source Nullable(String),
    localized_headline String,
    localized_summary String,
    key_points Array(String),
    positive_points Array(String),
    concerns Array(String),
    event_type LowCardinality(String),
    sentiment LowCardinality(String),
    impact_direction LowCardinality(String),
    why_it_matters String,
    model LowCardinality(String),
    localized_at DateTime64(3, 'UTC'),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(localized_at)
PARTITION BY toYYYYMM(published_at)
ORDER BY (symbol, locale, published_at, article_id)
TTL toDateTime(published_at) + INTERVAL 30 DAY DELETE;

CREATE TABLE IF NOT EXISTS market_data.news_company_daily_summaries
(
    date Date,
    symbol LowCardinality(String),
    locale LowCardinality(String),
    summary String,
    key_points Array(String),
    positive_points Array(String),
    concerns Array(String),
    impact_direction LowCardinality(String),
    sentiment LowCardinality(String),
    article_ids Array(String),
    article_ids_hash String,
    article_count UInt32,
    mention_count UInt32,
    status LowCardinality(String),
    model LowCardinality(String),
    generated_at DateTime64(3, 'UTC'),
    version LowCardinality(String),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(generated_at)
PARTITION BY toYYYYMM(date)
ORDER BY (symbol, locale, date, version)
TTL toDate(date) + INTERVAL 366 DAY DELETE;

CREATE TABLE IF NOT EXISTS market_data.load_audit
(
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3),
    source_name LowCardinality(String),
    object_path String,
    row_count UInt64,
    note String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(loaded_at)
ORDER BY (loaded_at, source_name);

CREATE TABLE IF NOT EXISTS market_data.backfill_jobs
(
    request_id String,
    symbol LowCardinality(String),
    interval LowCardinality(String),
    job_type LowCardinality(String),
    status LowCardinality(String),
    range_start DateTime64(3, 'UTC'),
    range_end DateTime64(3, 'UTC'),
    source_preference LowCardinality(String),
    object_paths Array(String),
    error Nullable(String),
    created_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC'),
    finished_at Nullable(DateTime64(3, 'UTC')),
    raw String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (request_id, symbol, interval);

CREATE TABLE IF NOT EXISTS market_data.storage_object_audit
(
    object_path String,
    bucket Nullable(String),
    dataset LowCardinality(String),
    layer LowCardinality(String),
    symbol Nullable(String),
    interval Nullable(String),
    object_format LowCardinality(String),
    row_count UInt64,
    checksum Nullable(String),
    source LowCardinality(String),
    created_at DateTime64(3, 'UTC'),
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (object_path, dataset, layer);

CREATE TABLE IF NOT EXISTS market_data.order_flow_profile_daily
(
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
    classification_version LowCardinality(String) DEFAULT 'orderflow-estimated-v2',
    source              LowCardinality(String) DEFAULT 'clickhouse-rollup',
    feed                LowCardinality(String) DEFAULT 'sip',
    feed_profile        LowCardinality(String) DEFAULT feed,
    market_session      LowCardinality(String) DEFAULT 'regular',
    inserted_at         DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(inserted_at)
PARTITION BY toYYYYMM(session_date)
ORDER BY (symbol, session_date, price_bin_size, price_bin);

CREATE TABLE IF NOT EXISTS market_data.chart_analysis_assets
(
    symbol         LowCardinality(String),
    interval       LowCardinality(String),
    as_of          DateTime64(3, 'UTC'),
    generated_at   DateTime64(3, 'UTC'),
    asset_version  LowCardinality(String),
    kernel_version LowCardinality(String),
    prompt_version LowCardinality(String) DEFAULT '',
    status         LowCardinality(String),
    payload        String,
    inserted_at    DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)
ORDER BY (symbol, interval);

ALTER TABLE market_data.chart_candles
    ADD COLUMN IF NOT EXISTS source_event_id Nullable(String) AFTER feed;

ALTER TABLE market_data.trade_ticks
    ADD COLUMN IF NOT EXISTS source_event_id Nullable(String) AFTER feed;

ALTER TABLE market_data.chart_candles
    ADD COLUMN IF NOT EXISTS feed_profile LowCardinality(String) DEFAULT feed AFTER feed,
    ADD COLUMN IF NOT EXISTS market_session LowCardinality(String) DEFAULT 'unknown' AFTER feed_profile;

ALTER TABLE market_data.chart_candles
    ADD COLUMN IF NOT EXISTS price_adjustment LowCardinality(String) DEFAULT 'unknown' AFTER market_session,
    ADD COLUMN IF NOT EXISTS canonical_version LowCardinality(String) DEFAULT 'legacy' AFTER price_adjustment,
    ADD COLUMN IF NOT EXISTS bucket_policy LowCardinality(String) DEFAULT 'clock_aligned' AFTER canonical_version;

ALTER TABLE market_data.chart_candles
    ADD COLUMN IF NOT EXISTS bucket_policy_key LowCardinality(String) AFTER bucket_policy,
    MODIFY ORDER BY (symbol, interval, event_time, feed_profile, market_session, bucket_policy_key);

ALTER TABLE market_data.trade_ticks
    ADD COLUMN IF NOT EXISTS feed_profile LowCardinality(String) DEFAULT feed AFTER feed,
    ADD COLUMN IF NOT EXISTS market_session LowCardinality(String) DEFAULT 'unknown' AFTER feed_profile;

ALTER TABLE market_data.quote_ticks
    ADD COLUMN IF NOT EXISTS feed_profile LowCardinality(String) DEFAULT feed AFTER feed,
    ADD COLUMN IF NOT EXISTS market_session LowCardinality(String) DEFAULT 'unknown' AFTER feed_profile;

ALTER TABLE market_data.market_status_events
    ADD COLUMN IF NOT EXISTS feed_profile LowCardinality(String) DEFAULT feed AFTER feed,
    ADD COLUMN IF NOT EXISTS market_session LowCardinality(String) DEFAULT 'unknown' AFTER feed_profile;

ALTER TABLE market_data.market_events
    ADD COLUMN IF NOT EXISTS feed_profile LowCardinality(String) DEFAULT feed AFTER feed,
    ADD COLUMN IF NOT EXISTS market_session LowCardinality(String) DEFAULT 'unknown' AFTER feed_profile;

ALTER TABLE market_data.trade_ticks MODIFY COLUMN IF EXISTS size Nullable(Float64);
ALTER TABLE market_data.quote_ticks MODIFY COLUMN IF EXISTS bid_size Nullable(Float64);
ALTER TABLE market_data.quote_ticks MODIFY COLUMN IF EXISTS ask_size Nullable(Float64);
ALTER TABLE market_data.chart_candles MODIFY COLUMN IF EXISTS volume Float64;
