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
