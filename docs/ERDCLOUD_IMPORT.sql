-- GOPS ERDCloud import DDL
-- Purpose: ERD visualization only. Do not execute this file against production.
-- Dialect: MySQL-compatible DDL chosen for ERDCloud import compatibility.
-- Mapping rules:
--   PostgreSQL TEXT -> VARCHAR/TEXT, JSONB -> JSON, TIMESTAMPTZ -> DATETIME(3)
--   ClickHouse String/LowCardinality -> VARCHAR/TEXT, Array/String payload -> JSON
--   ClickHouse tables use the ch_ prefix because ERDCloud has no ClickHouse engine model.
--   Redis keys, Kafka topics, S3 objects, and GraphDB triples are not relational tables.
--   PostgreSQL schema_migrations is technical bookkeeping and is intentionally omitted.
--   app_users is the internal owner key; OAuth subjects remain in user_identities during compatibility.
--   fk_logical_* constraints describe code-level relationships that ClickHouse does not enforce.

-- ============================================================================
-- PostgreSQL: order, alerts, recommendations, paper trading, chart assets
-- ============================================================================

CREATE TABLE app_users (
    app_user_id CHAR(36) NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (app_user_id)
);

CREATE TABLE user_identities (
    identity_id CHAR(36) NOT NULL,
    app_user_id CHAR(36) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    provider_subject VARCHAR(255) NOT NULL,
    email VARCHAR(320),
    email_verified BOOLEAN NOT NULL,
    display_name VARCHAR(255),
    picture_url TEXT,
    last_login_at DATETIME(3),
    created_at DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (identity_id),
    UNIQUE KEY uq_user_identities_provider_subject (provider, provider_subject),
    CONSTRAINT fk_user_identities_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id)
);

CREATE TABLE instruments (
    instrument_id CHAR(36) NOT NULL,
    canonical_symbol VARCHAR(64) NOT NULL,
    market VARCHAR(64),
    exchange VARCHAR(64),
    asset_type VARCHAR(64) NOT NULL,
    currency VARCHAR(16) NOT NULL,
    name VARCHAR(255),
    status VARCHAR(32) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (instrument_id),
    UNIQUE KEY uq_instruments_canonical_symbol (canonical_symbol)
);

CREATE TABLE instrument_aliases (
    alias_id CHAR(36) NOT NULL,
    instrument_id CHAR(36) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    provider_symbol VARCHAR(128) NOT NULL,
    valid_from DATETIME(3) NOT NULL,
    valid_to DATETIME(3),
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (alias_id),
    CONSTRAINT fk_instrument_aliases_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);

CREATE TABLE orders (
    order_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    client_order_id VARCHAR(128) NOT NULL,
    user_sub VARCHAR(255),
    app_user_id CHAR(36),
    account_alias VARCHAR(128) NOT NULL,
    market VARCHAR(32) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    instrument_id CHAR(36),
    side VARCHAR(16) NOT NULL,
    qty DECIMAL(24,8) NOT NULL,
    price DECIMAL(24,8) NOT NULL,
    exchange VARCHAR(32) NOT NULL,
    order_division VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    broker_order_id VARCHAR(128),
    reason TEXT,
    occurred_at VARCHAR(64) NOT NULL,
    occurred_at_ts DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (order_id),
    UNIQUE KEY uq_orders_request_id (request_id),
    UNIQUE KEY uq_orders_client_order_id (client_order_id),
    CONSTRAINT fk_orders_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id),
    CONSTRAINT fk_orders_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);

CREATE TABLE order_events (
    event_id VARCHAR(128) NOT NULL,
    order_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(128),
    client_order_id VARCHAR(128),
    account_alias VARCHAR(128),
    symbol VARCHAR(32),
    status VARCHAR(32) NOT NULL,
    reason TEXT,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (event_id),
    CONSTRAINT fk_order_events_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE idempotency_requests (
    key_hash VARCHAR(128) NOT NULL,
    body_hash VARCHAR(128) NOT NULL,
    order_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    response JSON,
    created_at DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (key_hash),
    CONSTRAINT fk_idempotency_requests_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE outbox_events (
    event_id VARCHAR(128) NOT NULL,
    topic VARCHAR(255) NOT NULL,
    message_key VARCHAR(255) NOT NULL,
    order_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    payload JSON NOT NULL,
    delivery_status VARCHAR(32) NOT NULL,
    attempt_count INT NOT NULL,
    next_attempt_at DATETIME(3) NOT NULL,
    last_error TEXT,
    locked_at DATETIME(3),
    lock_owner VARCHAR(255),
    published_at DATETIME(3),
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (event_id),
    CONSTRAINT fk_outbox_events_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE inbox_events (
    consumer_name VARCHAR(255) NOT NULL,
    event_id VARCHAR(128) NOT NULL,
    payload_digest VARCHAR(128),
    processed_at DATETIME(3) NOT NULL,
    PRIMARY KEY (consumer_name, event_id)
);

CREATE TABLE broker_submissions (
    submission_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    client_order_id VARCHAR(128) NOT NULL,
    order_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    redacted_command JSON NOT NULL,
    redacted_response JSON,
    reason TEXT,
    broker_order_id VARCHAR(128),
    created_at DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (submission_id),
    UNIQUE KEY uq_broker_submissions_request (request_id),
    UNIQUE KEY uq_broker_submissions_client_order (client_order_id),
    CONSTRAINT fk_broker_submissions_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE executions (
    execution_id VARCHAR(128) NOT NULL,
    order_id VARCHAR(128) NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (execution_id),
    CONSTRAINT fk_executions_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE dlq_events (
    id BIGINT NOT NULL AUTO_INCREMENT,
    source VARCHAR(255) NOT NULL,
    topic VARCHAR(255) NOT NULL,
    error_type VARCHAR(255) NOT NULL,
    error_message TEXT NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE reconciliation_runs (
    run_id VARCHAR(128) NOT NULL,
    account_alias VARCHAR(128),
    target_count INT NOT NULL,
    result_count INT NOT NULL,
    alert_count INT NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (run_id)
);

CREATE TABLE audit_logs (
    id BIGINT NOT NULL AUTO_INCREMENT,
    action VARCHAR(128) NOT NULL,
    order_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(128),
    client_order_id VARCHAR(128),
    account_alias VARCHAR(128),
    symbol VARCHAR(32),
    reason TEXT,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE alerts (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    symbol VARCHAR(32) NOT NULL,
    instrument_id CHAR(36),
    type VARCHAR(32) NOT NULL,
    direction VARCHAR(16),
    target_price DECIMAL(18,4),
    change_pct DECIMAL(6,2),
    window_min INT,
    `repeat` BOOLEAN NOT NULL,
    repeat_limit INT,
    triggered_count INT NOT NULL,
    notifications_enabled BOOLEAN NOT NULL,
    proposal_source VARCHAR(64),
    condition_version SMALLINT NOT NULL,
    `condition` JSON NOT NULL,
    created_via VARCHAR(32) NOT NULL,
    request_id VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    expires_at DATETIME(3),
    last_triggered_at DATETIME(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_alerts_user_request (user_sub, request_id),
    CONSTRAINT fk_alerts_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id),
    CONSTRAINT fk_alerts_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);

CREATE TABLE notifications (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    alert_id BIGINT,
    event_id VARCHAR(128) NOT NULL,
    type VARCHAR(64) NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME(3) NOT NULL,
    read_at DATETIME(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_notifications_event (event_id),
    CONSTRAINT fk_notifications_alert FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE SET NULL,
    CONSTRAINT fk_notifications_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id)
);

CREATE TABLE trade_conditions (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    source VARCHAR(32) NOT NULL,
    proposal_id VARCHAR(128),
    analysis_id VARCHAR(128),
    alert_id BIGINT NOT NULL,
    side VARCHAR(16) NOT NULL,
    limit_price DECIMAL(18,4) NOT NULL,
    quantity DECIMAL(18,4) NOT NULL,
    exchange VARCHAR(32) NOT NULL,
    execution_enabled BOOLEAN NOT NULL,
    execution_mode VARCHAR(32) NOT NULL,
    simulation_run_id VARCHAR(128),
    simulation_submitted_sequence BIGINT,
    status VARCHAR(32) NOT NULL,
    validity VARCHAR(32) NOT NULL,
    market_hours VARCHAR(32) NOT NULL,
    trigger_event_id VARCHAR(128),
    triggered_at DATETIME(3),
    order_id VARCHAR(128),
    paper_order_id VARCHAR(128),
    error_reason TEXT,
    version INT NOT NULL,
    created_at DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    expires_at DATETIME(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_trade_conditions_alert (alert_id),
    UNIQUE KEY uq_trade_conditions_user_proposal (user_sub, proposal_id),
    UNIQUE KEY uq_trade_conditions_trigger_event (trigger_event_id),
    CONSTRAINT fk_trade_conditions_alert FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE,
    CONSTRAINT fk_trade_conditions_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id)
);

CREATE TABLE user_notification_preferences (
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    settings JSON NOT NULL,
    company_overrides JSON NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (user_sub)
);

CREATE TABLE user_recommendation_score_profiles (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    name VARCHAR(255) NOT NULL,
    schema_version VARCHAR(64) NOT NULL,
    block_weights JSON NOT NULL,
    factor_weights JSON NOT NULL,
    portfolio_weight DECIMAL(6,2) NOT NULL,
    portfolio_factor_weights JSON NOT NULL,
    revision BIGINT NOT NULL,
    created_at DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_score_profiles_user_name (user_sub, name)
);

CREATE TABLE user_investment_profiles (
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    risk_level VARCHAR(32) NOT NULL,
    horizon VARCHAR(32) NOT NULL,
    max_drawdown_pct DECIMAL(6,2) NOT NULL,
    preferred_sectors JSON NOT NULL,
    excluded_sectors JSON NOT NULL,
    excluded_symbols JSON NOT NULL,
    recommendation_style VARCHAR(32) NOT NULL,
    active_score_profile_id BIGINT,
    profile_revision BIGINT NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (user_sub),
    CONSTRAINT fk_investment_profile_score_profile FOREIGN KEY (active_score_profile_id)
        REFERENCES user_recommendation_score_profiles(id) ON DELETE SET NULL
);

CREATE TABLE user_investment_profile_history (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    payload JSON NOT NULL,
    source_as_of DATETIME(3) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE user_layout_presets (
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    presets JSON NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (user_sub)
);

CREATE TABLE user_portfolio_snapshots (
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    payload JSON NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (user_sub)
);

CREATE TABLE user_portfolio_snapshot_history (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    payload JSON NOT NULL,
    source_as_of DATETIME(3) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE trade_decision_check_events (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    fill_id VARCHAR(128) NOT NULL,
    category VARCHAR(32) NOT NULL,
    label VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL,
    evidence JSON NOT NULL,
    source VARCHAR(255),
    source_as_of DATETIME(3),
    checked_at DATETIME(3),
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE order_coach_fill_history (
    id BIGINT NOT NULL AUTO_INCREMENT,
    fill_id VARCHAR(128) NOT NULL,
    observation_version BIGINT NOT NULL,
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    order_id VARCHAR(128) NOT NULL,
    source_execution_id VARCHAR(128),
    symbol VARCHAR(32) NOT NULL,
    instrument_id CHAR(36),
    side VARCHAR(16) NOT NULL,
    cumulative_filled_qty DECIMAL(24,8) NOT NULL,
    average_fill_price DECIMAL(24,8) NOT NULL,
    status VARCHAR(32) NOT NULL,
    decision_at DATETIME(3) NOT NULL,
    filled_at DATETIME(3) NOT NULL,
    source_observed_at DATETIME(3) NOT NULL,
    source_payload_digest VARCHAR(128) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_fill_history_version (fill_id, observation_version),
    UNIQUE KEY uq_fill_history_quantity (fill_id, cumulative_filled_qty),
    CONSTRAINT fk_fill_history_order FOREIGN KEY (order_id) REFERENCES orders(order_id),
    CONSTRAINT fk_fill_history_execution FOREIGN KEY (source_execution_id) REFERENCES executions(execution_id),
    CONSTRAINT fk_fill_history_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id),
    CONSTRAINT fk_fill_history_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);

CREATE TABLE stock_recommendation_evidence_snapshots (
    id BIGINT NOT NULL AUTO_INCREMENT,
    snapshot_key VARCHAR(255) NOT NULL,
    slot_start DATETIME(3) NOT NULL,
    market_date DATE NOT NULL,
    session_mode VARCHAR(32) NOT NULL,
    cutoff DATETIME(3) NOT NULL,
    universe JSON NOT NULL,
    rule_set_version VARCHAR(64) NOT NULL,
    source_digests JSON NOT NULL,
    source_status JSON NOT NULL,
    status VARCHAR(32) NOT NULL,
    input_digest VARCHAR(128) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_evidence_snapshots_key (snapshot_key)
);

CREATE TABLE stock_recommendation_evidence_candidates (
    id BIGINT NOT NULL AUTO_INCREMENT,
    snapshot_id BIGINT NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    instrument_id CHAR(36),
    sector VARCHAR(255) NOT NULL,
    industry VARCHAR(255) NOT NULL,
    change_percent DECIMAL(12,6),
    raw_factors JSON NOT NULL,
    normalized_factors JSON NOT NULL,
    block_scores JSON NOT NULL,
    base_setup_score DECIMAL(12,8) NOT NULL,
    evidence_reliability DECIMAL(12,8) NOT NULL,
    reliability_components JSON NOT NULL,
    rejection_reasons JSON NOT NULL,
    daily_returns_60 JSON NOT NULL,
    market_item JSON NOT NULL,
    narrative_context JSON NOT NULL,
    input_digest VARCHAR(128) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_evidence_candidates_snapshot_symbol (snapshot_id, symbol),
    CONSTRAINT fk_evidence_candidates_snapshot FOREIGN KEY (snapshot_id)
        REFERENCES stock_recommendation_evidence_snapshots(id) ON DELETE CASCADE,
    CONSTRAINT fk_evidence_candidates_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);

CREATE TABLE stock_recommendation_model_registry (
    model_version VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    training_cutoff DATETIME(3) NOT NULL,
    feature_definitions JSON NOT NULL,
    weights JSON NOT NULL,
    validation_report JSON NOT NULL,
    created_at DATETIME(3) NOT NULL,
    activated_at DATETIME(3),
    PRIMARY KEY (model_version)
);

CREATE TABLE stock_recommendation_runs (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_sub VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    run_key VARCHAR(255) NOT NULL,
    slot_start VARCHAR(64) NOT NULL,
    slot_start_ts DATETIME(3) NOT NULL,
    market_date VARCHAR(32) NOT NULL,
    market_date_value DATE NOT NULL,
    status VARCHAR(32) NOT NULL,
    profile_snapshot JSON NOT NULL,
    market_snapshot_time VARCHAR(64) NOT NULL,
    market_snapshot_at DATETIME(3) NOT NULL,
    summary JSON NOT NULL,
    generated_at DATETIME(3) NOT NULL,
    portfolio_snapshot_history_id BIGINT,
    weights_version VARCHAR(64) NOT NULL,
    algorithm_version VARCHAR(64) NOT NULL,
    fundamental_snapshot_provenance JSON NOT NULL,
    evidence_snapshot_id BIGINT,
    scoring_input_digest VARCHAR(128),
    scoring_snapshot JSON NOT NULL,
    model_version VARCHAR(128),
    PRIMARY KEY (id),
    UNIQUE KEY uq_recommendation_runs_user_key (user_sub, run_key),
    CONSTRAINT fk_recommendation_runs_portfolio_history FOREIGN KEY (portfolio_snapshot_history_id)
        REFERENCES user_portfolio_snapshot_history(id),
    CONSTRAINT fk_recommendation_runs_evidence_snapshot FOREIGN KEY (evidence_snapshot_id)
        REFERENCES stock_recommendation_evidence_snapshots(id),
    CONSTRAINT fk_recommendation_runs_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id),
    CONSTRAINT fk_recommendation_runs_model FOREIGN KEY (model_version)
        REFERENCES stock_recommendation_model_registry(model_version)
);

CREATE TABLE stock_recommendation_items (
    id BIGINT NOT NULL AUTO_INCREMENT,
    run_id BIGINT NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    instrument_id CHAR(36),
    action VARCHAR(32) NOT NULL,
    `rank` INT NOT NULL,
    score DECIMAL(6,2) NOT NULL,
    confidence DECIMAL(5,4) NOT NULL,
    sector VARCHAR(255),
    reasons JSON NOT NULL,
    risk_warnings JSON NOT NULL,
    metrics_snapshot JSON NOT NULL,
    explanation_json JSON,
    decision_json JSON NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_recommendation_items_run FOREIGN KEY (run_id)
        REFERENCES stock_recommendation_runs(id) ON DELETE CASCADE,
    CONSTRAINT fk_recommendation_items_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);

CREATE TABLE stock_recommendation_outcomes (
    id BIGINT NOT NULL AUTO_INCREMENT,
    recommendation_item_id BIGINT NOT NULL,
    label_market_date DATE NOT NULL,
    symbol_open DECIMAL(20,8) NOT NULL,
    symbol_close DECIMAL(20,8) NOT NULL,
    spy_open DECIMAL(20,8) NOT NULL,
    spy_close DECIMAL(20,8) NOT NULL,
    open_to_close_excess_return_pct DECIMAL(12,8) NOT NULL,
    label_version VARCHAR(64) NOT NULL,
    observed_at DATETIME(3) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_recommendation_outcomes_item (recommendation_item_id),
    CONSTRAINT fk_recommendation_outcomes_item FOREIGN KEY (recommendation_item_id)
        REFERENCES stock_recommendation_items(id) ON DELETE CASCADE
);

CREATE TABLE paper_accounts (
    user_id VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    current_generation INT NOT NULL,
    currency VARCHAR(16) NOT NULL,
    seed_profile VARCHAR(128),
    seeded_at DATETIME(3),
    seed_suppressed_at DATETIME(3),
    created_at DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_paper_accounts_app_user (app_user_id),
    CONSTRAINT fk_paper_accounts_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id)
);

CREATE TABLE paper_account_runs (
    user_id VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    generation INT NOT NULL,
    starting_cash DECIMAL(24,6) NOT NULL,
    cash_balance DECIMAL(24,6) NOT NULL,
    reserved_cash DECIMAL(24,6) NOT NULL,
    status VARCHAR(32) NOT NULL,
    started_at DATETIME(3) NOT NULL,
    ended_at DATETIME(3),
    PRIMARY KEY (user_id, generation),
    CONSTRAINT fk_paper_account_runs_account FOREIGN KEY (user_id) REFERENCES paper_accounts(user_id)
);

CREATE TABLE paper_positions (
    user_id VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    generation INT NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    instrument_id CHAR(36),
    qty DECIMAL(24,0) NOT NULL,
    reserved_qty DECIMAL(24,0) NOT NULL,
    average_price DECIMAL(24,6) NOT NULL,
    realized_pnl DECIMAL(24,6) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (user_id, generation, symbol),
    CONSTRAINT fk_paper_positions_run FOREIGN KEY (user_id, generation)
        REFERENCES paper_account_runs(user_id, generation),
    CONSTRAINT fk_paper_positions_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);

CREATE TABLE paper_orders (
    order_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    generation INT NOT NULL,
    market VARCHAR(32) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    instrument_id CHAR(36),
    side VARCHAR(16) NOT NULL,
    qty DECIMAL(24,0) NOT NULL,
    limit_price DECIMAL(24,6) NOT NULL,
    exchange VARCHAR(32) NOT NULL,
    order_division VARCHAR(32) NOT NULL,
    order_type VARCHAR(16) NOT NULL,
    execution_mode VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    filled_qty DECIMAL(24,0) NOT NULL,
    fill_price DECIMAL(24,6),
    quote_event_id VARCHAR(128),
    quote_timestamp DATETIME(3),
    reason TEXT,
    idempotency_key_hash VARCHAR(128) NOT NULL,
    body_hash VARCHAR(128) NOT NULL,
    simulation_run_id VARCHAR(128),
    simulation_submitted_sequence BIGINT,
    virtual_submitted_at DATETIME(3),
    virtual_filled_at DATETIME(3),
    seed_profile VARCHAR(128),
    created_at DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    filled_at DATETIME(3),
    cancelled_at DATETIME(3),
    PRIMARY KEY (order_id),
    UNIQUE KEY uq_paper_orders_user_idempotency (user_id, idempotency_key_hash),
    CONSTRAINT fk_paper_orders_run FOREIGN KEY (user_id, generation)
        REFERENCES paper_account_runs(user_id, generation),
    CONSTRAINT fk_paper_orders_user FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id),
    CONSTRAINT fk_paper_orders_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);

CREATE TABLE paper_executions (
    execution_id VARCHAR(128) NOT NULL,
    order_id VARCHAR(128) NOT NULL,
    execution_sequence INT NOT NULL,
    quantity DECIMAL(24,0) NOT NULL,
    price DECIMAL(24,6) NOT NULL,
    fee DECIMAL(24,6) NOT NULL,
    quote_event_id VARCHAR(128),
    quote_timestamp DATETIME(3),
    executed_at DATETIME(3) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (execution_id),
    UNIQUE KEY uq_paper_executions_order_sequence (order_id, execution_sequence),
    CONSTRAINT fk_paper_executions_order FOREIGN KEY (order_id) REFERENCES paper_orders(order_id)
);

CREATE TABLE paper_order_events (
    event_id VARCHAR(128) NOT NULL,
    order_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    generation INT NOT NULL,
    execution_id VARCHAR(128),
    event_type VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    reason TEXT,
    payload JSON NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (event_id),
    CONSTRAINT fk_paper_order_events_order FOREIGN KEY (order_id) REFERENCES paper_orders(order_id),
    CONSTRAINT fk_paper_order_events_execution FOREIGN KEY (execution_id) REFERENCES paper_executions(execution_id)
);

CREATE TABLE paper_cash_ledger (
    entry_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(255) NOT NULL,
    app_user_id CHAR(36),
    generation INT NOT NULL,
    order_id VARCHAR(128),
    execution_id VARCHAR(128),
    event_type VARCHAR(64) NOT NULL,
    cash_delta DECIMAL(24,6) NOT NULL,
    reserved_cash_delta DECIMAL(24,6) NOT NULL,
    cash_balance_after DECIMAL(24,6) NOT NULL,
    reserved_cash_after DECIMAL(24,6) NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (entry_id),
    CONSTRAINT fk_paper_cash_ledger_run FOREIGN KEY (user_id, generation)
        REFERENCES paper_account_runs(user_id, generation),
    CONSTRAINT fk_paper_cash_ledger_order FOREIGN KEY (order_id) REFERENCES paper_orders(order_id),
    CONSTRAINT fk_paper_cash_ledger_execution FOREIGN KEY (execution_id) REFERENCES paper_executions(execution_id)
);

ALTER TABLE trade_conditions
    ADD CONSTRAINT fk_trade_conditions_paper_order
    FOREIGN KEY (paper_order_id) REFERENCES paper_orders(order_id);

ALTER TABLE paper_account_runs ADD CONSTRAINT fk_paper_account_runs_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);
ALTER TABLE paper_positions ADD CONSTRAINT fk_paper_positions_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);
ALTER TABLE paper_order_events ADD CONSTRAINT fk_paper_order_events_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);
ALTER TABLE paper_cash_ledger ADD CONSTRAINT fk_paper_cash_ledger_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);
ALTER TABLE user_notification_preferences ADD CONSTRAINT fk_notification_preferences_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);
ALTER TABLE user_recommendation_score_profiles ADD CONSTRAINT fk_score_profiles_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);
ALTER TABLE user_investment_profiles ADD CONSTRAINT fk_investment_profiles_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);
ALTER TABLE user_investment_profile_history ADD CONSTRAINT fk_investment_profile_history_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);
ALTER TABLE user_layout_presets ADD CONSTRAINT fk_layout_presets_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);
ALTER TABLE user_portfolio_snapshots ADD CONSTRAINT fk_portfolio_snapshots_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);
ALTER TABLE user_portfolio_snapshot_history ADD CONSTRAINT fk_portfolio_snapshot_history_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);
ALTER TABLE trade_decision_check_events ADD CONSTRAINT fk_decision_events_user
    FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id);

CREATE TABLE simulation_matcher_checkpoints (
    matcher_id VARCHAR(128) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    `sequence` BIGINT NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (matcher_id)
);

CREATE TABLE chart_assets_analysis_assets (
    symbol VARCHAR(32) NOT NULL,
    `interval` VARCHAR(16) NOT NULL,
    as_of DATETIME(3) NOT NULL,
    generated_at DATETIME(3) NOT NULL,
    asset_version VARCHAR(64) NOT NULL,
    kernel_version VARCHAR(64) NOT NULL,
    prompt_version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    quality_state VARCHAR(32),
    drawing_count SMALLINT NOT NULL,
    payload_bytes INT NOT NULL,
    asset_content_digest VARCHAR(128),
    payload_digest VARCHAR(128) NOT NULL,
    payload JSON NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, `interval`)
);

CREATE TABLE chart_assets_geometry_assets (
    symbol VARCHAR(32) NOT NULL,
    `interval` VARCHAR(16) NOT NULL,
    as_of DATETIME(3) NOT NULL,
    generated_at DATETIME(3) NOT NULL,
    asset_version VARCHAR(64) NOT NULL,
    algorithm_version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    coverage_state VARCHAR(32) NOT NULL,
    drawing_count SMALLINT NOT NULL,
    payload_bytes INT NOT NULL,
    input_digest VARCHAR(128) NOT NULL,
    payload_digest VARCHAR(128) NOT NULL,
    payload JSON NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, `interval`)
);

CREATE TABLE chart_assets_geometry_build_jobs (
    job_id VARCHAR(128) NOT NULL,
    requested_by VARCHAR(255) NOT NULL,
    submitted_at DATETIME(3) NOT NULL,
    status VARCHAR(32) NOT NULL,
    force_build BOOLEAN NOT NULL,
    source VARCHAR(32) NOT NULL,
    priority SMALLINT NOT NULL,
    request_fingerprint VARCHAR(255) NOT NULL,
    requested_intervals JSON NOT NULL,
    symbol_count INT NOT NULL,
    total_items INT NOT NULL,
    cancel_requested BOOLEAN NOT NULL,
    repair JSON NOT NULL,
    logs JSON NOT NULL,
    created_entities INT NOT NULL,
    build_target VARCHAR(32) NOT NULL,
    simulation_dataset_id VARCHAR(128),
    simulation_cutoff DATETIME(3),
    started_at DATETIME(3),
    finished_at DATETIME(3),
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (job_id)
);

CREATE TABLE chart_assets_geometry_build_items (
    job_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    `interval` VARCHAR(16) NOT NULL,
    status VARCHAR(32) NOT NULL,
    stage VARCHAR(64) NOT NULL,
    attempts SMALLINT NOT NULL,
    worker_id VARCHAR(128),
    lease_expires_at DATETIME(3),
    error TEXT,
    warning TEXT,
    reason TEXT,
    elapsed_ms INT NOT NULL,
    created_entities SMALLINT NOT NULL,
    started_at DATETIME(3),
    finished_at DATETIME(3),
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (job_id, symbol, `interval`),
    CONSTRAINT fk_geometry_build_items_job FOREIGN KEY (job_id)
        REFERENCES chart_assets_geometry_build_jobs(job_id) ON DELETE CASCADE
);

CREATE TABLE chart_assets_geometry_asset_snapshots (
    dataset_id VARCHAR(128) NOT NULL,
    snapshot_cutoff DATETIME(3) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    `interval` VARCHAR(16) NOT NULL,
    as_of DATETIME(3) NOT NULL,
    generated_at DATETIME(3) NOT NULL,
    asset_version VARCHAR(64) NOT NULL,
    algorithm_version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    coverage_state VARCHAR(32) NOT NULL,
    drawing_count SMALLINT NOT NULL,
    payload_bytes INT NOT NULL,
    input_digest VARCHAR(128) NOT NULL,
    payload_digest VARCHAR(128) NOT NULL,
    payload JSON NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (dataset_id, symbol, `interval`)
);

-- ============================================================================
-- ClickHouse logical model: market data, news, fundamentals, company journal
-- ch_ primary keys represent ClickHouse ORDER BY identity for ERD visualization.
-- ============================================================================

CREATE TABLE ch_symbols (
    symbol VARCHAR(32) NOT NULL,
    name VARCHAR(255) NOT NULL,
    exchange VARCHAR(64),
    market VARCHAR(64) NOT NULL,
    asset_class VARCHAR(64) NOT NULL,
    tradable BOOLEAN NOT NULL,
    status VARCHAR(32) NOT NULL,
    source VARCHAR(64) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    raw JSON,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol)
);

CREATE TABLE ch_simulation_replay_datasets (
    dataset_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    start_time DATETIME(6) NOT NULL,
    end_time DATETIME(6) NOT NULL,
    total_events BIGINT UNSIGNED NOT NULL,
    total_trades BIGINT UNSIGNED NOT NULL,
    total_quotes BIGINT UNSIGNED NOT NULL,
    manifest JSON NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (dataset_id)
);

CREATE TABLE ch_simulation_replay_staging (
    dataset_id VARCHAR(128) NOT NULL,
    event_time DATETIME(6) NOT NULL,
    source_file VARCHAR(512) NOT NULL,
    source_sequence BIGINT UNSIGNED NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    feed VARCHAR(32) NOT NULL,
    payload JSON NOT NULL,
    PRIMARY KEY (dataset_id, event_time, source_file, source_sequence),
    CONSTRAINT fk_logical_replay_staging_dataset FOREIGN KEY (dataset_id)
        REFERENCES ch_simulation_replay_datasets(dataset_id),
    CONSTRAINT fk_logical_replay_staging_symbol FOREIGN KEY (symbol)
        REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_simulation_replay_events (
    dataset_id VARCHAR(128) NOT NULL,
    event_time DATETIME(6) NOT NULL,
    `sequence` BIGINT UNSIGNED NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    feed VARCHAR(32) NOT NULL,
    payload JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (dataset_id, `sequence`),
    CONSTRAINT fk_logical_replay_events_dataset FOREIGN KEY (dataset_id)
        REFERENCES ch_simulation_replay_datasets(dataset_id),
    CONSTRAINT fk_logical_replay_events_symbol FOREIGN KEY (symbol)
        REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_simulation_replay_candles_1m (
    dataset_id VARCHAR(128) NOT NULL,
    event_time DATETIME(3) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    `open` DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    `close` DOUBLE NOT NULL,
    volume DOUBLE NOT NULL,
    trade_count BIGINT UNSIGNED NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (dataset_id, symbol, event_time),
    CONSTRAINT fk_logical_replay_candles_dataset FOREIGN KEY (dataset_id)
        REFERENCES ch_simulation_replay_datasets(dataset_id),
    CONSTRAINT fk_logical_replay_candles_symbol FOREIGN KEY (symbol)
        REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_trade_ticks (
    event_time DATETIME(3) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    trade_id BIGINT UNSIGNED NOT NULL,
    price DOUBLE NOT NULL,
    size DOUBLE,
    exchange VARCHAR(64),
    conditions JSON NOT NULL,
    tape VARCHAR(32),
    source VARCHAR(64) NOT NULL,
    feed VARCHAR(32) NOT NULL,
    feed_profile VARCHAR(32) NOT NULL,
    market_session VARCHAR(32) NOT NULL,
    source_event_id VARCHAR(255),
    received_at DATETIME(3),
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, event_time, feed_profile, trade_id),
    CONSTRAINT fk_logical_trade_ticks_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_quote_ticks (
    event_time DATETIME(3) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    bid_price DOUBLE,
    bid_size DOUBLE,
    ask_price DOUBLE,
    ask_size DOUBLE,
    bid_exchange VARCHAR(64),
    ask_exchange VARCHAR(64),
    conditions JSON NOT NULL,
    source VARCHAR(64) NOT NULL,
    feed VARCHAR(32) NOT NULL,
    feed_profile VARCHAR(32) NOT NULL,
    market_session VARCHAR(32) NOT NULL,
    source_event_id VARCHAR(255),
    received_at DATETIME(3),
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, event_time, feed_profile),
    CONSTRAINT fk_logical_quote_ticks_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_chart_candles (
    event_time DATETIME(3) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    `interval` VARCHAR(16) NOT NULL,
    `open` DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    `close` DOUBLE NOT NULL,
    volume DOUBLE NOT NULL,
    trade_count BIGINT UNSIGNED,
    vwap DOUBLE,
    ma5 DOUBLE,
    ma20 DOUBLE,
    ma60 DOUBLE,
    is_closed BOOLEAN NOT NULL,
    correction_type VARCHAR(32) NOT NULL,
    source VARCHAR(64) NOT NULL,
    feed VARCHAR(32) NOT NULL,
    feed_profile VARCHAR(32) NOT NULL,
    market_session VARCHAR(32) NOT NULL,
    price_adjustment VARCHAR(32) NOT NULL,
    canonical_version VARCHAR(32) NOT NULL,
    bucket_policy VARCHAR(64) NOT NULL,
    bucket_policy_key VARCHAR(64) NOT NULL,
    source_event_id VARCHAR(255),
    created_at DATETIME(3),
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, `interval`, event_time, feed_profile, market_session, bucket_policy_key),
    CONSTRAINT fk_logical_chart_candles_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_market_status_events (
    event_time DATETIME(3) NOT NULL,
    symbol VARCHAR(32),
    status_type VARCHAR(64) NOT NULL,
    status VARCHAR(255) NOT NULL,
    reason TEXT,
    source VARCHAR(64) NOT NULL,
    feed VARCHAR(32) NOT NULL,
    feed_profile VARCHAR(32) NOT NULL,
    market_session VARCHAR(32) NOT NULL,
    source_event_id VARCHAR(255),
    raw JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    CONSTRAINT fk_logical_market_status_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_market_events (
    event_time DATETIME(3) NOT NULL,
    symbol VARCHAR(32),
    event_type VARCHAR(64) NOT NULL,
    `layer` VARCHAR(64) NOT NULL,
    source VARCHAR(64) NOT NULL,
    feed VARCHAR(32) NOT NULL,
    feed_profile VARCHAR(32) NOT NULL,
    market_session VARCHAR(32) NOT NULL,
    source_event_id VARCHAR(255),
    payload JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    CONSTRAINT fk_logical_market_events_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_agent_graph_expansions (
    symbol VARCHAR(32) NOT NULL,
    relation_version VARCHAR(64) NOT NULL,
    generated_at DATETIME(3) NOT NULL,
    payload JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, relation_version, generated_at),
    CONSTRAINT fk_logical_graph_expansions_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_news_articles (
    published_at DATETIME(3) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    article_id VARCHAR(255) NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT,
    content LONGTEXT,
    url TEXT,
    source VARCHAR(255),
    author VARCHAR(255),
    updated_at DATETIME(3),
    received_at DATETIME(3),
    raw JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, published_at, article_id),
    CONSTRAINT fk_logical_news_articles_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_news_article_localizations (
    published_at DATETIME(3) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    article_id VARCHAR(255) NOT NULL,
    locale VARCHAR(16) NOT NULL,
    symbols JSON NOT NULL,
    target_symbol VARCHAR(32) NOT NULL,
    subject_relevance VARCHAR(32) NOT NULL,
    relevance_score_v2 FLOAT NOT NULL,
    relevance_reason TEXT NOT NULL,
    direct_signals_json JSON NOT NULL,
    headline TEXT,
    summary TEXT,
    url TEXT,
    source VARCHAR(255),
    localized_headline TEXT NOT NULL,
    localized_summary TEXT NOT NULL,
    key_points JSON NOT NULL,
    positive_points JSON NOT NULL,
    concerns JSON NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    sentiment VARCHAR(32) NOT NULL,
    impact_direction VARCHAR(32) NOT NULL,
    why_it_matters TEXT NOT NULL,
    model VARCHAR(128) NOT NULL,
    localized_at DATETIME(3) NOT NULL,
    raw JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, locale, published_at, article_id),
    CONSTRAINT fk_logical_localizations_article FOREIGN KEY (symbol, published_at, article_id)
        REFERENCES ch_news_articles(symbol, published_at, article_id),
    CONSTRAINT fk_logical_localizations_target_symbol FOREIGN KEY (target_symbol)
        REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_news_company_daily_summaries (
    `date` DATE NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    locale VARCHAR(16) NOT NULL,
    summary TEXT NOT NULL,
    key_points JSON NOT NULL,
    positive_points JSON NOT NULL,
    concerns JSON NOT NULL,
    impact_direction VARCHAR(32) NOT NULL,
    sentiment VARCHAR(32) NOT NULL,
    article_ids JSON NOT NULL,
    article_ids_hash VARCHAR(128) NOT NULL,
    article_count INT UNSIGNED NOT NULL,
    mention_count INT UNSIGNED NOT NULL,
    status VARCHAR(32) NOT NULL,
    model VARCHAR(128) NOT NULL,
    generated_at DATETIME(3) NOT NULL,
    version VARCHAR(64) NOT NULL,
    raw JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, locale, `date`, version),
    CONSTRAINT fk_logical_daily_summaries_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_load_audit (
    loaded_at DATETIME(3) NOT NULL,
    source_name VARCHAR(128) NOT NULL,
    object_path TEXT NOT NULL,
    row_count BIGINT UNSIGNED NOT NULL,
    note TEXT NOT NULL
);

CREATE TABLE ch_backfill_jobs (
    request_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    `interval` VARCHAR(16) NOT NULL,
    job_type VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    range_start DATETIME(3) NOT NULL,
    range_end DATETIME(3) NOT NULL,
    source_preference VARCHAR(64) NOT NULL,
    object_paths JSON NOT NULL,
    error TEXT,
    created_at DATETIME(3) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    finished_at DATETIME(3),
    raw JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (request_id, symbol, `interval`),
    CONSTRAINT fk_logical_backfill_jobs_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_storage_object_audit (
    object_path VARCHAR(512) NOT NULL,
    bucket VARCHAR(255),
    dataset VARCHAR(64) NOT NULL,
    `layer` VARCHAR(64) NOT NULL,
    symbol VARCHAR(32),
    `interval` VARCHAR(16),
    object_format VARCHAR(32) NOT NULL,
    row_count BIGINT UNSIGNED NOT NULL,
    checksum VARCHAR(128),
    source VARCHAR(64) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (object_path, dataset, `layer`),
    CONSTRAINT fk_logical_storage_audit_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_order_flow_profile_daily (
    session_date DATE NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    price_bin DOUBLE NOT NULL,
    price_bin_size DOUBLE NOT NULL,
    ask_volume DOUBLE NOT NULL,
    bid_volume DOUBLE NOT NULL,
    unknown_volume DOUBLE NOT NULL,
    ask_trade_count BIGINT UNSIGNED NOT NULL,
    bid_trade_count BIGINT UNSIGNED NOT NULL,
    unknown_trade_count BIGINT UNSIGNED NOT NULL,
    trade_count BIGINT UNSIGNED NOT NULL,
    volume DOUBLE NOT NULL,
    classification_version VARCHAR(64) NOT NULL,
    source VARCHAR(64) NOT NULL,
    feed VARCHAR(32) NOT NULL,
    feed_profile VARCHAR(32) NOT NULL,
    market_session VARCHAR(32) NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, session_date, price_bin_size, price_bin),
    CONSTRAINT fk_logical_order_flow_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_chart_analysis_assets (
    symbol VARCHAR(32) NOT NULL,
    `interval` VARCHAR(16) NOT NULL,
    as_of DATETIME(3) NOT NULL,
    generated_at DATETIME(3) NOT NULL,
    asset_version VARCHAR(64) NOT NULL,
    kernel_version VARCHAR(64) NOT NULL,
    prompt_version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    payload JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, `interval`),
    CONSTRAINT fk_logical_chart_analysis_assets_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_sec_company_tickers (
    symbol VARCHAR(32) NOT NULL,
    cik VARCHAR(32) NOT NULL,
    company_name VARCHAR(255) NOT NULL,
    exchange VARCHAR(64) NOT NULL,
    is_active_universe_member BOOLEAN NOT NULL,
    universe_source VARCHAR(255) NOT NULL,
    updated_at DATETIME(3) NOT NULL,
    raw JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol),
    UNIQUE KEY uq_ch_sec_company_tickers_cik (cik),
    CONSTRAINT fk_logical_sec_tickers_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_sec_filing_events (
    symbol VARCHAR(32) NOT NULL,
    cik VARCHAR(32) NOT NULL,
    form VARCHAR(32) NOT NULL,
    filed_at DATE NOT NULL,
    accession VARCHAR(64) NOT NULL,
    items JSON NOT NULL,
    event_only BOOLEAN NOT NULL,
    raw JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, form, filed_at, accession),
    CONSTRAINT fk_logical_sec_filings_company FOREIGN KEY (symbol)
        REFERENCES ch_sec_company_tickers(symbol)
);

CREATE TABLE ch_sec_raw_artifacts (
    symbol VARCHAR(32) NOT NULL,
    cik VARCHAR(32) NOT NULL,
    artifact_type VARCHAR(64) NOT NULL,
    object_path VARCHAR(512) NOT NULL,
    checksum VARCHAR(128) NOT NULL,
    source_url TEXT NOT NULL,
    collected_at DATETIME(3) NOT NULL,
    raw JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, artifact_type, object_path),
    CONSTRAINT fk_logical_sec_artifacts_company FOREIGN KEY (symbol)
        REFERENCES ch_sec_company_tickers(symbol)
);

CREATE TABLE ch_sec_financial_facts (
    symbol VARCHAR(32) NOT NULL,
    cik VARCHAR(32) NOT NULL,
    metric VARCHAR(128) NOT NULL,
    taxonomy VARCHAR(64) NOT NULL,
    concept VARCHAR(255) NOT NULL,
    unit VARCHAR(32) NOT NULL,
    `value` DOUBLE,
    fiscal_year SMALLINT UNSIGNED NOT NULL,
    fiscal_period VARCHAR(32) NOT NULL,
    period_end DATE NOT NULL,
    form VARCHAR(32) NOT NULL,
    accession VARCHAR(64),
    filed_at DATE NOT NULL,
    quality VARCHAR(32) NOT NULL,
    raw JSON NOT NULL,
    version_filed_at DATE NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, metric, unit, fiscal_year, fiscal_period, period_end),
    CONSTRAINT fk_logical_sec_facts_company FOREIGN KEY (symbol)
        REFERENCES ch_sec_company_tickers(symbol)
);

CREATE TABLE ch_sec_derived_metrics (
    symbol VARCHAR(32) NOT NULL,
    cik VARCHAR(32) NOT NULL,
    metric VARCHAR(128) NOT NULL,
    `value` DOUBLE,
    fiscal_year SMALLINT UNSIGNED NOT NULL,
    fiscal_period VARCHAR(32) NOT NULL,
    period_end DATE NOT NULL,
    form VARCHAR(32) NOT NULL,
    accession VARCHAR(64),
    filed_at DATE NOT NULL,
    quality VARCHAR(32) NOT NULL,
    raw JSON NOT NULL,
    version_filed_at DATE NOT NULL,
    computed_at DATETIME(3) NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, metric, fiscal_year, fiscal_period, period_end),
    CONSTRAINT fk_logical_sec_metrics_company FOREIGN KEY (symbol)
        REFERENCES ch_sec_company_tickers(symbol)
);

CREATE TABLE ch_sec_frames (
    frame_period VARCHAR(32) NOT NULL,
    taxonomy VARCHAR(64) NOT NULL,
    concept VARCHAR(255) NOT NULL,
    unit VARCHAR(32) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    cik VARCHAR(32) NOT NULL,
    `value` DOUBLE,
    accession VARCHAR(64) NOT NULL,
    filed_at DATE NOT NULL,
    quality VARCHAR(32) NOT NULL,
    raw JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (frame_period, taxonomy, concept, unit, symbol),
    CONSTRAINT fk_logical_sec_frames_company FOREIGN KEY (symbol)
        REFERENCES ch_sec_company_tickers(symbol)
);

CREATE TABLE ch_sec_collection_runs (
    run_id VARCHAR(128) NOT NULL,
    job_type VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    symbol_count INT UNSIGNED NOT NULL,
    started_at DATETIME(3) NOT NULL,
    finished_at DATETIME(3),
    raw JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (job_type, run_id)
);

CREATE TABLE ch_yahoo_earnings_estimates (
    symbol VARCHAR(32) NOT NULL,
    metric VARCHAR(128) NOT NULL,
    fiscal_year SMALLINT UNSIGNED NOT NULL,
    fiscal_period VARCHAR(32) NOT NULL,
    period_end DATE NOT NULL,
    average DOUBLE,
    low DOUBLE,
    high DOUBLE,
    analyst_count SMALLINT UNSIGNED,
    event_at DATETIME(3),
    actual_value DOUBLE,
    surprise_percent DOUBLE,
    event_session VARCHAR(32) NOT NULL,
    event_status VARCHAR(32) NOT NULL,
    source VARCHAR(64) NOT NULL,
    collected_at DATETIME(3) NOT NULL,
    raw JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, metric, fiscal_year, fiscal_period, period_end),
    CONSTRAINT fk_logical_yahoo_estimates_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_yahoo_analyst_summaries (
    symbol VARCHAR(32) NOT NULL,
    statement TEXT NOT NULL,
    tone VARCHAR(32) NOT NULL,
    source_as_of DATETIME(3),
    replay_statement TEXT NOT NULL,
    replay_tone VARCHAR(32) NOT NULL,
    replay_source_as_of DATETIME(3),
    replay_cutoff DATETIME(3),
    source VARCHAR(64) NOT NULL,
    collected_at DATETIME(3) NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol),
    CONSTRAINT fk_logical_yahoo_summaries_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_company_journal_reports_v1 (
    symbol VARCHAR(32) NOT NULL,
    analysis_as_of DATE NOT NULL,
    generated_at DATETIME(3) NOT NULL,
    input_digest VARCHAR(128) NOT NULL,
    contract_version VARCHAR(64) NOT NULL,
    headline TEXT NOT NULL,
    keywords JSON NOT NULL,
    recent_movement TEXT NOT NULL,
    financial_stability TEXT NOT NULL,
    watch_items TEXT NOT NULL,
    tab_narratives_json JSON NOT NULL,
    server_metrics_json JSON NOT NULL,
    news_ids JSON NOT NULL,
    sec_filing_ids JSON NOT NULL,
    price_as_of DATE,
    graph_relation_ids JSON NOT NULL,
    missing_data JSON NOT NULL,
    validation_status VARCHAR(32) NOT NULL,
    validation_errors JSON NOT NULL,
    model VARCHAR(128) NOT NULL,
    prompt_version VARCHAR(64) NOT NULL,
    source_receipt_json JSON NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (symbol, analysis_as_of, input_digest, generated_at),
    CONSTRAINT fk_logical_company_journal_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

CREATE TABLE ch_company_journal_generation_events_v1 (
    request_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    analysis_as_of DATE NOT NULL,
    input_digest VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    requested_source VARCHAR(64) NOT NULL,
    error TEXT,
    occurred_at DATETIME(3) NOT NULL,
    inserted_at DATETIME(3) NOT NULL,
    PRIMARY KEY (request_id, occurred_at),
    CONSTRAINT fk_logical_company_journal_events_symbol FOREIGN KEY (symbol) REFERENCES ch_symbols(symbol)
);

-- PostgreSQL↔ClickHouse cross-store logical edges. These FOREIGN KEY clauses are
-- visualization-only; ClickHouse never enforces them physically. Actual table names
-- are market_data.<name>; ch_ is only this ERDCloud document's display alias.
ALTER TABLE ch_symbols ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_symbols ADD CONSTRAINT fk_cross_symbols_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_simulation_replay_staging ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_simulation_replay_staging ADD CONSTRAINT fk_cross_replay_staging_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_simulation_replay_events ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_simulation_replay_events ADD CONSTRAINT fk_cross_replay_events_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_simulation_replay_candles_1m ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_simulation_replay_candles_1m ADD CONSTRAINT fk_cross_replay_candles_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_trade_ticks ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_trade_ticks ADD CONSTRAINT fk_cross_trade_ticks_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_quote_ticks ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_quote_ticks ADD CONSTRAINT fk_cross_quote_ticks_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_chart_candles ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_chart_candles ADD CONSTRAINT fk_cross_chart_candles_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_market_status_events ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_market_status_events ADD CONSTRAINT fk_cross_market_status_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_market_events ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_market_events ADD CONSTRAINT fk_cross_market_events_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_agent_graph_expansions ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_agent_graph_expansions ADD CONSTRAINT fk_cross_graph_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_news_articles ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_news_articles ADD CONSTRAINT fk_cross_news_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_news_article_localizations ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_news_article_localizations ADD CONSTRAINT fk_cross_news_localizations_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_news_company_daily_summaries ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_news_company_daily_summaries ADD CONSTRAINT fk_cross_news_summary_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_backfill_jobs ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_backfill_jobs ADD CONSTRAINT fk_cross_backfill_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_order_flow_profile_daily ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_order_flow_profile_daily ADD CONSTRAINT fk_cross_orderflow_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_chart_analysis_assets ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_chart_analysis_assets ADD CONSTRAINT fk_cross_chart_assets_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_sec_company_tickers ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_sec_company_tickers ADD CONSTRAINT fk_cross_sec_tickers_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_sec_filing_events ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_sec_filing_events ADD CONSTRAINT fk_cross_sec_filings_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_sec_raw_artifacts ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_sec_raw_artifacts ADD CONSTRAINT fk_cross_sec_artifacts_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_sec_financial_facts ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_sec_financial_facts ADD CONSTRAINT fk_cross_sec_facts_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_sec_derived_metrics ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_sec_derived_metrics ADD CONSTRAINT fk_cross_sec_derived_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_sec_frames ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_sec_frames ADD CONSTRAINT fk_cross_sec_frames_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_yahoo_earnings_estimates ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_yahoo_earnings_estimates ADD CONSTRAINT fk_cross_yahoo_estimates_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_yahoo_analyst_summaries ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_yahoo_analyst_summaries ADD CONSTRAINT fk_cross_yahoo_summary_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_company_journal_reports_v1 ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_company_journal_reports_v1 ADD COLUMN schema_version VARCHAR(64);
ALTER TABLE ch_company_journal_reports_v1 ADD CONSTRAINT fk_cross_company_journal_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
ALTER TABLE ch_company_journal_generation_events_v1 ADD COLUMN instrument_id CHAR(36);
ALTER TABLE ch_company_journal_generation_events_v1 ADD COLUMN schema_version VARCHAR(64);
ALTER TABLE ch_company_journal_generation_events_v1 ADD CONSTRAINT fk_cross_company_journal_events_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id);
