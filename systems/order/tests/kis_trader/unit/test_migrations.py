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


def test_recommendation_migration_declares_profile_runs_and_items():
    [migration] = [path for path in migration_files() if path.name == "0004_recommendations.sql"]
    sql = migration.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS user_investment_profiles" in sql
    assert "CREATE TABLE IF NOT EXISTS stock_recommendation_runs" in sql
    assert "CREATE TABLE IF NOT EXISTS stock_recommendation_items" in sql
    assert "CREATE TABLE IF NOT EXISTS user_portfolio_snapshots" in sql
    assert "stock_recommendation_runs_user_key_unique" in sql


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
