-- Fresh-database bootstrap for logical PostgreSQL↔ClickHouse instrument links.
-- The versioned clickhouse-migrations job also applies these ALTERs to existing DBs
-- and installs the deterministic *_latest views.
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
