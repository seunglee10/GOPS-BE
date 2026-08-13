-- ERDCloud/MySQL display DDL. Production source: PostgreSQL public schema.
CREATE TABLE app_users (
  app_user_id CHAR(36) PRIMARY KEY, status VARCHAR(32) NOT NULL,
  created_at DATETIME(3) NOT NULL, updated_at DATETIME(3) NOT NULL
);
CREATE TABLE user_identities (
  identity_id CHAR(36) PRIMARY KEY, app_user_id CHAR(36) NOT NULL,
  provider VARCHAR(64) NOT NULL, provider_subject VARCHAR(255) NOT NULL,
  email VARCHAR(320), email_verified BOOLEAN NOT NULL, last_login_at DATETIME(3),
  UNIQUE KEY uq_identity_provider_subject (provider, provider_subject),
  CONSTRAINT fk_identity_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id)
);
CREATE TABLE instruments (
  instrument_id CHAR(36) PRIMARY KEY, canonical_symbol VARCHAR(64) NOT NULL,
  market VARCHAR(64), exchange VARCHAR(64), status VARCHAR(32) NOT NULL
);
CREATE TABLE instrument_aliases (
  alias_id CHAR(36) PRIMARY KEY, instrument_id CHAR(36) NOT NULL,
  provider VARCHAR(64) NOT NULL, provider_symbol VARCHAR(128) NOT NULL,
  valid_from DATETIME(3) NOT NULL, valid_to DATETIME(3),
  CONSTRAINT fk_alias_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE stock_recommendation_model_registry (
  model_version VARCHAR(128) PRIMARY KEY, status VARCHAR(32) NOT NULL,
  training_cutoff DATETIME(3) NOT NULL, weights JSON NOT NULL
);
CREATE TABLE stock_recommendation_runs (
  id BIGINT PRIMARY KEY, app_user_id CHAR(36), user_sub VARCHAR(255) NOT NULL,
  run_key VARCHAR(255) NOT NULL, slot_start VARCHAR(64) NOT NULL,
  slot_start_ts DATETIME(3) NOT NULL, market_date VARCHAR(32) NOT NULL,
  market_date_value DATE NOT NULL, market_snapshot_time VARCHAR(64) NOT NULL,
  market_snapshot_at DATETIME(3) NOT NULL, algorithm_version VARCHAR(64) NOT NULL,
  weights_version VARCHAR(64) NOT NULL, model_version VARCHAR(128), status VARCHAR(32) NOT NULL,
  CONSTRAINT fk_run_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id),
  CONSTRAINT fk_run_model FOREIGN KEY (model_version) REFERENCES stock_recommendation_model_registry(model_version)
);
CREATE TABLE stock_recommendation_items (
  id BIGINT PRIMARY KEY, run_id BIGINT NOT NULL, instrument_id CHAR(36),
  symbol VARCHAR(64) NOT NULL, `rank` INT NOT NULL, score DECIMAL(6,2) NOT NULL,
  confidence DECIMAL(5,4) NOT NULL,
  CONSTRAINT fk_item_run FOREIGN KEY (run_id) REFERENCES stock_recommendation_runs(id),
  CONSTRAINT fk_item_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE user_investment_profiles (
  user_sub VARCHAR(255) PRIMARY KEY, app_user_id CHAR(36), risk_level VARCHAR(32) NOT NULL,
  recommendation_style VARCHAR(32) NOT NULL, active_score_profile_id BIGINT,
  CONSTRAINT fk_profile_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id)
);
