-- ERDCloud display aliases. Actual names are market_data.*; no physical FK exists.
CREATE TABLE instruments (
  instrument_id CHAR(36) PRIMARY KEY, canonical_symbol VARCHAR(64) NOT NULL
);
CREATE TABLE ch_symbols (
  symbol VARCHAR(64) PRIMARY KEY, instrument_id CHAR(36), name VARCHAR(255) NOT NULL,
  exchange VARCHAR(64), market VARCHAR(64) NOT NULL, updated_at DATETIME(3) NOT NULL,
  CONSTRAINT fk_cross_symbols_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE ch_chart_candles (
  symbol VARCHAR(64) NOT NULL, instrument_id CHAR(36), `interval` VARCHAR(16) NOT NULL,
  event_time DATETIME(3) NOT NULL, feed_profile VARCHAR(64) NOT NULL,
  market_session VARCHAR(32) NOT NULL, bucket_policy_key VARCHAR(128) NOT NULL,
  close DOUBLE NOT NULL, inserted_at DATETIME(3) NOT NULL,
  PRIMARY KEY (symbol, `interval`, event_time, feed_profile, market_session, bucket_policy_key),
  CONSTRAINT fk_cross_candle_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE ch_news_articles (
  symbol VARCHAR(64) NOT NULL, instrument_id CHAR(36), article_id VARCHAR(255) NOT NULL,
  published_at DATETIME(3) NOT NULL, headline TEXT NOT NULL, inserted_at DATETIME(3) NOT NULL,
  PRIMARY KEY (symbol, published_at, article_id),
  CONSTRAINT fk_cross_news_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE ch_sec_company_tickers (
  symbol VARCHAR(64) PRIMARY KEY, instrument_id CHAR(36), cik VARCHAR(32) NOT NULL,
  company_name VARCHAR(255) NOT NULL, updated_at DATETIME(3) NOT NULL,
  CONSTRAINT fk_cross_sec_ticker_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE ch_sec_financial_facts (
  symbol VARCHAR(64) NOT NULL, instrument_id CHAR(36), metric VARCHAR(128) NOT NULL,
  fiscal_year INT NOT NULL, fiscal_period VARCHAR(32) NOT NULL, period_end DATE NOT NULL,
  value DOUBLE, version_filed_at DATE NOT NULL,
  PRIMARY KEY (symbol, metric, fiscal_year, fiscal_period, period_end),
  CONSTRAINT fk_cross_sec_fact_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE ch_company_journal_reports_v1 (
  symbol VARCHAR(64) NOT NULL, instrument_id CHAR(36), schema_version VARCHAR(64) NOT NULL,
  analysis_as_of DATE NOT NULL, input_digest VARCHAR(128) NOT NULL,
  generated_at DATETIME(3) NOT NULL, validation_status VARCHAR(32) NOT NULL,
  PRIMARY KEY (symbol, analysis_as_of, input_digest, generated_at),
  CONSTRAINT fk_cross_journal_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);
