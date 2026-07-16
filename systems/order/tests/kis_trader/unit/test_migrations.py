from kis_trader.persistence.migrations import migration_files


def test_initial_migration_declares_required_order_tables():
    [initial] = [path for path in migration_files() if path.name == "0001_orders.sql"]
    sql = initial.read_text(encoding="utf-8")

    for table_name in [
        "orders",
        "order_events",
        "idempotency_requests",
        "outbox_events",
        "broker_submissions",
        "executions",
        "dlq_events",
        "reconciliation_runs",
        "audit_logs",
    ]:
        assert f"CREATE TABLE IF NOT EXISTS {table_name}" in sql


def test_repository_does_not_auto_create_schema():
    from kis_trader.persistence.postgres import PostgresOrderRepository

    assert not hasattr(PostgresOrderRepository, "init_schema")


def test_ai_coach_migration_declares_owned_orders_and_point_in_time_sources():
    [migration] = [path for path in migration_files() if path.name == "0006_ai_coach.sql"]
    sql = migration.read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS user_sub" in sql
    assert "CREATE TABLE IF NOT EXISTS user_portfolio_snapshot_history" in sql
    assert "CREATE TABLE IF NOT EXISTS trade_decision_check_events" in sql


def test_ai_coach_execution_lookup_has_join_and_time_index():
    [migration] = [
        path for path in migration_files() if path.name == "0007_ai_coach_execution_index.sql"
    ]
    sql = migration.read_text(encoding="utf-8")

    assert "CREATE INDEX IF NOT EXISTS idx_executions_order_id_created_at" in sql
    assert "ON executions (order_id, created_at)" in sql


def test_alert_migration_declares_alert_and_notification_tables():
    [migration] = [path for path in migration_files() if path.name == "0002_alerts.sql"]
    sql = migration.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS alerts" in sql
    assert "CREATE TABLE IF NOT EXISTS notifications" in sql
    assert "CONSTRAINT alerts_price_cross_shape" in sql
    assert "CONSTRAINT alerts_spike_shape" in sql
    assert "event_id TEXT NOT NULL UNIQUE" in sql


def test_alert_repeat_limit_migration_tracks_trigger_counts():
    [migration] = [path for path in migration_files() if path.name == "0003_alert_repeat_limits.sql"]
    sql = migration.read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS repeat_limit INTEGER" in sql
    assert "ADD COLUMN IF NOT EXISTS triggered_count INTEGER NOT NULL DEFAULT 0" in sql
    assert "CONSTRAINT alerts_repeat_limit_check" in sql
    assert "CONSTRAINT alerts_triggered_count_check" in sql


def test_alert_proposal_source_migration_preserves_ai_coach_origin():
    [migration] = [path for path in migration_files() if path.name == "0008_alert_proposal_source.sql"]
    sql = migration.read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS proposal_source TEXT" in sql
    assert "CONSTRAINT alerts_proposal_source_check" in sql
    for source in ("daily_trade", "entry_habit", "exit_habit", "portfolio_risk"):
        assert source in sql


def test_recommendation_migration_declares_profile_runs_and_items():
    [migration] = [path for path in migration_files() if path.name == "0004_recommendations.sql"]
    sql = migration.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS user_investment_profiles" in sql
    assert "CREATE TABLE IF NOT EXISTS stock_recommendation_runs" in sql
    assert "CREATE TABLE IF NOT EXISTS stock_recommendation_items" in sql
    assert "CREATE TABLE IF NOT EXISTS user_portfolio_snapshots" in sql
    assert "stock_recommendation_runs_user_key_unique" in sql


def test_personalized_recommendation_migration_versions_inputs_and_snapshot_reference():
    [migration] = [path for path in migration_files() if path.name == "0011_personalized_recommendations.sql"]
    sql = migration.read_text(encoding="utf-8")

    assert "recommendation_style" in sql
    assert "portfolio_snapshot_history_id" in sql
    assert "weights_version" in sql
    assert "personalization_input_digest" in sql
    assert "personalization_snapshot" in sql
    assert "CREATE TABLE IF NOT EXISTS stock_recommendation_model_registry" in sql
    assert "CREATE TABLE IF NOT EXISTS stock_recommendation_outcomes" in sql
    assert "open_to_close_excess_return_pct" in sql


def test_continuous_recommendation_v2_migration_declares_state_and_candidate_features():
    [migration] = [
        path for path in migration_files() if path.name == "0012_continuous_recommendation_v2.sql"
    ]
    sql = migration.read_text(encoding="utf-8")

    assert "algorithm_version" in sql
    assert "CREATE TABLE IF NOT EXISTS user_recommendation_preference_states" in sql
    assert "CREATE TABLE IF NOT EXISTS user_recommendation_preference_events" in sql
    assert "CREATE TABLE IF NOT EXISTS user_recommendation_risk_states" in sql
    assert "CREATE TABLE IF NOT EXISTS stock_recommendation_candidate_features" in sql
    assert "CREATE TABLE IF NOT EXISTS order_coach_fill_history" in sql
    assert "fill_history_id BIGINT NOT NULL UNIQUE REFERENCES order_coach_fill_history" in sql
    assert "provenance JSONB NOT NULL" in sql
    assert "INSERT INTO order_coach_fill_history" in sql
    assert "o.user_sub IS NOT NULL" in sql
    assert "fundamental_weight >= 0 AND fundamental_weight <= 0.15" in sql


def test_paper_trading_migration_declares_isolated_account_and_order_tables():
    [migration] = [path for path in migration_files() if path.name == "0006_paper_trading.sql"]
    sql = migration.read_text(encoding="utf-8")

    for table_name in [
        "paper_accounts",
        "paper_account_runs",
        "paper_positions",
        "paper_orders",
        "paper_order_events",
        "paper_cash_ledger",
    ]:
        assert f"CREATE TABLE IF NOT EXISTS {table_name}" in sql
    assert "UNIQUE (user_id, idempotency_key_hash)" in sql
    assert "WHERE status = 'pending'" in sql


def test_trade_condition_migration_declares_durable_conditions_and_alert_delivery_flag():
    [migration] = [path for path in migration_files() if path.name == "0008_trade_conditions.sql"]
    sql = migration.read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS notifications_enabled" in sql
    assert "CREATE TABLE IF NOT EXISTS trade_conditions" in sql
    assert "trade_conditions_user_proposal_unique" in sql
    assert "trade_conditions_trigger_event_unique" in sql


def test_notification_preferences_migration_declares_user_scoped_json_settings():
    [migration] = [path for path in migration_files() if path.name == "0009_notification_preferences.sql"]
    sql = migration.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS user_notification_preferences" in sql
    assert "user_sub TEXT PRIMARY KEY" in sql
    assert "settings JSONB NOT NULL" in sql
    assert "company_overrides JSONB NOT NULL" in sql
