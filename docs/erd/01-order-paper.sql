-- ERDCloud/MySQL display DDL. Production source: PostgreSQL public schema.
CREATE TABLE app_users (
  app_user_id CHAR(36) PRIMARY KEY, status VARCHAR(32) NOT NULL,
  created_at DATETIME(3) NOT NULL, updated_at DATETIME(3) NOT NULL
);
CREATE TABLE instruments (
  instrument_id CHAR(36) PRIMARY KEY, canonical_symbol VARCHAR(64) NOT NULL,
  market VARCHAR(64), exchange VARCHAR(64), asset_type VARCHAR(64) NOT NULL,
  currency VARCHAR(16) NOT NULL, name VARCHAR(255), status VARCHAR(32) NOT NULL
);
CREATE TABLE orders (
  order_id VARCHAR(128) PRIMARY KEY, app_user_id CHAR(36), user_sub VARCHAR(255),
  instrument_id CHAR(36), symbol VARCHAR(64) NOT NULL, side VARCHAR(16) NOT NULL,
  qty DECIMAL(24,8) NOT NULL, price DECIMAL(24,8) NOT NULL, status VARCHAR(32) NOT NULL,
  occurred_at VARCHAR(64) NOT NULL, occurred_at_ts DATETIME(3) NOT NULL,
  CONSTRAINT fk_orders_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id),
  CONSTRAINT fk_orders_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE outbox_events (
  event_id VARCHAR(128) PRIMARY KEY, order_id VARCHAR(128) NOT NULL,
  topic VARCHAR(255) NOT NULL, message_key VARCHAR(255) NOT NULL, payload JSON NOT NULL,
  delivery_status VARCHAR(32) NOT NULL, attempt_count INT NOT NULL,
  next_attempt_at DATETIME(3) NOT NULL, last_error TEXT, locked_at DATETIME(3),
  lock_owner VARCHAR(255), published_at DATETIME(3),
  CONSTRAINT fk_outbox_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
CREATE TABLE inbox_events (
  consumer_name VARCHAR(255) NOT NULL, event_id VARCHAR(128) NOT NULL,
  payload_digest VARCHAR(128), processed_at DATETIME(3) NOT NULL,
  PRIMARY KEY (consumer_name, event_id)
);
CREATE TABLE paper_accounts (
  user_id VARCHAR(255) PRIMARY KEY, app_user_id CHAR(36), current_generation INT NOT NULL,
  currency VARCHAR(16) NOT NULL,
  CONSTRAINT fk_paper_account_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id)
);
CREATE TABLE paper_orders (
  order_id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(255) NOT NULL, app_user_id CHAR(36),
  instrument_id CHAR(36), symbol VARCHAR(64) NOT NULL, side VARCHAR(16) NOT NULL,
  qty DECIMAL(24,0) NOT NULL, limit_price DECIMAL(24,6) NOT NULL,
  status VARCHAR(32) NOT NULL, filled_qty DECIMAL(24,0) NOT NULL, fill_price DECIMAL(24,6),
  CONSTRAINT fk_paper_order_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id),
  CONSTRAINT fk_paper_order_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE paper_executions (
  execution_id VARCHAR(128) PRIMARY KEY, order_id VARCHAR(128) NOT NULL,
  execution_sequence INT NOT NULL, quantity DECIMAL(24,0) NOT NULL,
  price DECIMAL(24,6) NOT NULL, fee DECIMAL(24,6) NOT NULL,
  quote_event_id VARCHAR(128), quote_timestamp DATETIME(3), executed_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_execution_sequence (order_id, execution_sequence),
  CONSTRAINT fk_execution_order FOREIGN KEY (order_id) REFERENCES paper_orders(order_id)
);
CREATE TABLE paper_order_events (
  event_id VARCHAR(128) PRIMARY KEY, order_id VARCHAR(128) NOT NULL,
  execution_id VARCHAR(128), event_type VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL,
  CONSTRAINT fk_paper_event_order FOREIGN KEY (order_id) REFERENCES paper_orders(order_id),
  CONSTRAINT fk_paper_event_execution FOREIGN KEY (execution_id) REFERENCES paper_executions(execution_id)
);
CREATE TABLE paper_cash_ledger (
  entry_id VARCHAR(128) PRIMARY KEY, order_id VARCHAR(128), execution_id VARCHAR(128),
  cash_delta DECIMAL(24,6) NOT NULL, reserved_cash_delta DECIMAL(24,6) NOT NULL,
  cash_balance_after DECIMAL(24,6) NOT NULL,
  CONSTRAINT fk_cash_order FOREIGN KEY (order_id) REFERENCES paper_orders(order_id),
  CONSTRAINT fk_cash_execution FOREIGN KEY (execution_id) REFERENCES paper_executions(execution_id)
);
CREATE TABLE trade_conditions (
  id BIGINT PRIMARY KEY, app_user_id CHAR(36), alert_id BIGINT NOT NULL,
  paper_order_id VARCHAR(128), status VARCHAR(32) NOT NULL,
  CONSTRAINT fk_condition_order FOREIGN KEY (paper_order_id) REFERENCES paper_orders(order_id)
);
