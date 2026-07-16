CREATE INDEX IF NOT EXISTS idx_executions_order_id_created_at
    ON executions (order_id, created_at);
