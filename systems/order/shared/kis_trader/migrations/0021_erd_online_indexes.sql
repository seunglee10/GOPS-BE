-- migration: nontransactional
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_occurred_at_ts ON orders (occurred_at_ts DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_recommendation_runs_slot_start_ts ON stock_recommendation_runs (slot_start_ts DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_recommendation_runs_market_date_value ON stock_recommendation_runs (market_date_value DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_idempotency_requests_order_id ON idempotency_requests (order_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_broker_submissions_order_id ON broker_submissions (order_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_notifications_alert_id ON notifications (alert_id) WHERE alert_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_paper_cash_ledger_order_id ON paper_cash_ledger (order_id) WHERE order_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_coach_fill_source_execution_id ON order_coach_fill_history (source_execution_id) WHERE source_execution_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_recommendation_runs_evidence_snapshot_id ON stock_recommendation_runs (evidence_snapshot_id) WHERE evidence_snapshot_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_investment_profiles_active_score_profile_id ON user_investment_profiles (active_score_profile_id) WHERE active_score_profile_id IS NOT NULL;
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS trade_conditions_paper_order_unique ON trade_conditions (paper_order_id) WHERE paper_order_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_paper_executions_order_executed ON paper_executions (order_id, executed_at, execution_sequence);
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_paper_executions_quote_event ON paper_executions (order_id, quote_event_id) WHERE quote_event_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_outbox_claimable ON outbox_events (next_attempt_at, created_at) WHERE published_at IS NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_app_user_occurred ON orders (app_user_id, occurred_at_ts DESC) WHERE app_user_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_instrument_created ON orders (instrument_id, occurred_at_ts DESC) WHERE instrument_id IS NOT NULL;
