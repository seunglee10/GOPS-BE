ALTER TABLE market_data.simulation_replay_staging ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.simulation_replay_events ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.simulation_replay_candles_1m ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.trade_ticks ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.quote_ticks ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.chart_candles ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.market_status_events ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.market_events ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.symbols ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.agent_graph_expansions ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.news_articles ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.news_article_localizations ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.news_company_daily_summaries ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.backfill_jobs ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.order_flow_profile_daily ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.chart_analysis_assets ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.sec_company_tickers ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.sec_filing_events ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.sec_raw_artifacts ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.sec_financial_facts ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.sec_derived_metrics ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.sec_frames ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.yahoo_analyst_summaries ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.company_journal_reports_v1 ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.company_journal_reports_v1 ADD COLUMN IF NOT EXISTS schema_version LowCardinality(String) DEFAULT 'company-journal.v2';
ALTER TABLE market_data.company_journal_generation_events_v1 ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID);
ALTER TABLE market_data.company_journal_generation_events_v1 ADD COLUMN IF NOT EXISTS schema_version LowCardinality(String) DEFAULT 'company-journal.v2';

CREATE OR REPLACE VIEW market_data.symbols_latest AS
SELECT
    symbol,
    argMax(name, tuple(src.inserted_at, src.updated_at, cityHash64(toString(src.raw)))) AS name,
    argMax(exchange, tuple(src.inserted_at, src.updated_at, cityHash64(toString(src.raw)))) AS exchange,
    argMax(market, tuple(src.inserted_at, src.updated_at, cityHash64(toString(src.raw)))) AS market,
    argMax(asset_class, tuple(src.inserted_at, src.updated_at, cityHash64(toString(src.raw)))) AS asset_class,
    argMax(tradable, tuple(src.inserted_at, src.updated_at, cityHash64(toString(src.raw)))) AS tradable,
    argMax(status, tuple(src.inserted_at, src.updated_at, cityHash64(toString(src.raw)))) AS status,
    argMax(source, tuple(src.inserted_at, src.updated_at, cityHash64(toString(src.raw)))) AS source,
    argMax(updated_at, tuple(src.inserted_at, src.updated_at, cityHash64(toString(src.raw)))) AS updated_at,
    argMax(raw, tuple(src.inserted_at, src.updated_at, cityHash64(toString(src.raw)))) AS raw,
    argMax(instrument_id, tuple(src.inserted_at, src.updated_at, cityHash64(toString(src.raw)))) AS instrument_id,
    max(inserted_at) AS inserted_at
FROM market_data.symbols AS src
GROUP BY symbol;

CREATE OR REPLACE VIEW market_data.chart_candles_latest AS
SELECT
    event_time, symbol, interval, feed_profile, market_session, bucket_policy_key,
    argMax(open, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS open,
    argMax(high, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS high,
    argMax(low, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS low,
    argMax(close, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS close,
    argMax(volume, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS volume,
    argMax(trade_count, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS trade_count,
    argMax(vwap, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS vwap,
    argMax(ma5, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS ma5,
    argMax(ma20, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS ma20,
    argMax(ma60, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS ma60,
    argMax(is_closed, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS is_closed,
    argMax(correction_type, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS correction_type,
    argMax(source, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS source,
    argMax(feed, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS feed,
    argMax(price_adjustment, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS price_adjustment,
    argMax(canonical_version, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS canonical_version,
    argMax(bucket_policy, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS bucket_policy,
    argMax(source_event_id, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS source_event_id,
    argMax(created_at, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS created_at,
    argMax(instrument_id, tuple(src.inserted_at, coalesce(src.source_event_id, ''), src.row_hash)) AS instrument_id,
    max(inserted_at) AS inserted_at
FROM (
    SELECT *, cityHash64(toString(tuple(open, high, low, close, volume, coalesce(source_event_id, '')))) AS row_hash
    FROM market_data.chart_candles
) AS src
GROUP BY event_time, symbol, interval, feed_profile, market_session, bucket_policy_key;

CREATE OR REPLACE VIEW market_data.sec_company_tickers_latest AS
SELECT
    symbol,
    argMax(cik, tuple(src.updated_at, src.inserted_at, cityHash64(src.raw))) AS cik,
    argMax(company_name, tuple(src.updated_at, src.inserted_at, cityHash64(src.raw))) AS company_name,
    argMax(exchange, tuple(src.updated_at, src.inserted_at, cityHash64(src.raw))) AS exchange,
    argMax(is_active_universe_member, tuple(src.updated_at, src.inserted_at, cityHash64(src.raw))) AS is_active_universe_member,
    argMax(universe_source, tuple(src.updated_at, src.inserted_at, cityHash64(src.raw))) AS universe_source,
    max(updated_at) AS updated_at,
    argMax(raw, tuple(src.updated_at, src.inserted_at, cityHash64(src.raw))) AS raw,
    argMax(instrument_id, tuple(src.updated_at, src.inserted_at, cityHash64(src.raw))) AS instrument_id,
    max(inserted_at) AS inserted_at
FROM market_data.sec_company_tickers AS src
GROUP BY symbol;

CREATE OR REPLACE VIEW market_data.yahoo_analyst_summaries_latest AS
SELECT
    symbol,
    argMax(statement, tuple(src.collected_at, src.inserted_at, cityHash64(src.statement))) AS statement,
    argMax(tone, tuple(src.collected_at, src.inserted_at, cityHash64(src.statement))) AS tone,
    argMax(source_as_of, tuple(src.collected_at, src.inserted_at, cityHash64(src.statement))) AS source_as_of,
    argMax(replay_statement, tuple(src.collected_at, src.inserted_at, cityHash64(src.statement))) AS replay_statement,
    argMax(replay_tone, tuple(src.collected_at, src.inserted_at, cityHash64(src.statement))) AS replay_tone,
    argMax(replay_source_as_of, tuple(src.collected_at, src.inserted_at, cityHash64(src.statement))) AS replay_source_as_of,
    argMax(replay_cutoff, tuple(src.collected_at, src.inserted_at, cityHash64(src.statement))) AS replay_cutoff,
    argMax(source, tuple(src.collected_at, src.inserted_at, cityHash64(src.statement))) AS source,
    max(collected_at) AS collected_at,
    argMax(instrument_id, tuple(src.collected_at, src.inserted_at, cityHash64(src.statement))) AS instrument_id,
    max(inserted_at) AS inserted_at
FROM market_data.yahoo_analyst_summaries AS src
GROUP BY symbol;

CREATE OR REPLACE VIEW market_data.simulation_replay_datasets_latest AS
SELECT * FROM market_data.simulation_replay_datasets
ORDER BY updated_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY dataset_id;

CREATE OR REPLACE VIEW market_data.simulation_replay_candles_1m_latest AS
SELECT * FROM market_data.simulation_replay_candles_1m
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY dataset_id, symbol, event_time;

CREATE OR REPLACE VIEW market_data.market_status_events_latest AS
SELECT * FROM market_data.market_status_events
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY coalesce(symbol, '_MARKET'), status_type, event_time, feed_profile, market_session;

CREATE OR REPLACE VIEW market_data.market_events_latest AS
SELECT * FROM market_data.market_events
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY coalesce(symbol, '_MARKET'), event_type, event_time, feed_profile, market_session;

CREATE OR REPLACE VIEW market_data.agent_graph_expansions_latest AS
SELECT * FROM market_data.agent_graph_expansions
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, relation_version, generated_at;

CREATE OR REPLACE VIEW market_data.news_articles_latest AS
SELECT * FROM market_data.news_articles
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, published_at, article_id;

CREATE OR REPLACE VIEW market_data.news_article_localizations_latest AS
SELECT * FROM market_data.news_article_localizations
ORDER BY localized_at DESC, inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, locale, published_at, article_id;

CREATE OR REPLACE VIEW market_data.news_company_daily_summaries_latest AS
SELECT * FROM market_data.news_company_daily_summaries
ORDER BY generated_at DESC, inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, locale, date, version;

CREATE OR REPLACE VIEW market_data.backfill_jobs_latest AS
SELECT * FROM market_data.backfill_jobs
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY request_id, symbol, interval;

CREATE OR REPLACE VIEW market_data.storage_object_audit_latest AS
SELECT * FROM market_data.storage_object_audit
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY object_path, dataset, layer;

CREATE OR REPLACE VIEW market_data.order_flow_profile_daily_latest AS
SELECT * FROM market_data.order_flow_profile_daily
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, session_date, price_bin_size, price_bin;

CREATE OR REPLACE VIEW market_data.chart_analysis_assets_latest AS
SELECT * FROM market_data.chart_analysis_assets
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, interval;

CREATE OR REPLACE VIEW market_data.sec_filing_events_latest AS
SELECT * FROM market_data.sec_filing_events
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, form, filed_at, accession;

CREATE OR REPLACE VIEW market_data.sec_raw_artifacts_latest AS
SELECT * FROM market_data.sec_raw_artifacts
ORDER BY collected_at DESC, inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, artifact_type, object_path;

CREATE OR REPLACE VIEW market_data.sec_financial_facts_latest AS
SELECT * FROM market_data.sec_financial_facts
ORDER BY version_filed_at DESC, inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, metric, unit, fiscal_year, fiscal_period, period_end;

CREATE OR REPLACE VIEW market_data.sec_derived_metrics_latest AS
SELECT * FROM market_data.sec_derived_metrics
ORDER BY version_filed_at DESC, inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, metric, fiscal_year, fiscal_period, period_end;

CREATE OR REPLACE VIEW market_data.sec_frames_latest AS
SELECT * FROM market_data.sec_frames
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY frame_period, taxonomy, concept, unit, symbol;

CREATE OR REPLACE VIEW market_data.sec_collection_runs_latest AS
SELECT * FROM market_data.sec_collection_runs
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY job_type, run_id;

CREATE OR REPLACE VIEW market_data.yahoo_earnings_estimates_latest AS
SELECT * FROM market_data.yahoo_earnings_estimates
ORDER BY collected_at DESC, inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, metric, fiscal_year, fiscal_period, period_end;

CREATE OR REPLACE VIEW market_data.company_journal_reports AS
SELECT * FROM market_data.company_journal_reports_v1;

CREATE OR REPLACE VIEW market_data.company_journal_generation_events AS
SELECT * FROM market_data.company_journal_generation_events_v1;
