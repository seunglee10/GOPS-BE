ALTER TABLE paper_accounts
    ADD COLUMN IF NOT EXISTS seed_profile TEXT,
    ADD COLUMN IF NOT EXISTS seeded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS seed_suppressed_at TIMESTAMPTZ;

ALTER TABLE paper_orders
    ADD COLUMN IF NOT EXISTS order_type TEXT NOT NULL DEFAULT 'limit',
    ADD COLUMN IF NOT EXISTS execution_mode TEXT NOT NULL DEFAULT 'paper',
    ADD COLUMN IF NOT EXISTS simulation_run_id TEXT,
    ADD COLUMN IF NOT EXISTS simulation_submitted_sequence BIGINT,
    ADD COLUMN IF NOT EXISTS virtual_submitted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS virtual_filled_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS seed_profile TEXT;

ALTER TABLE paper_orders DROP CONSTRAINT IF EXISTS paper_orders_order_type_check;
ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_order_type_check
    CHECK (order_type IN ('limit', 'market'));
ALTER TABLE paper_orders DROP CONSTRAINT IF EXISTS paper_orders_execution_mode_check;
ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_execution_mode_check
    CHECK (execution_mode IN ('paper', 'simulation'));

CREATE INDEX IF NOT EXISTS idx_paper_orders_simulation_pending
    ON paper_orders (simulation_run_id, simulation_submitted_sequence, created_at)
    WHERE execution_mode = 'simulation' AND status = 'pending';

ALTER TABLE trade_conditions
    ADD COLUMN IF NOT EXISTS execution_mode TEXT NOT NULL DEFAULT 'paper',
    ADD COLUMN IF NOT EXISTS simulation_run_id TEXT,
    ADD COLUMN IF NOT EXISTS simulation_submitted_sequence BIGINT;

ALTER TABLE trade_conditions DROP CONSTRAINT IF EXISTS trade_conditions_execution_mode_check;
ALTER TABLE trade_conditions ADD CONSTRAINT trade_conditions_execution_mode_check
    CHECK (execution_mode IN ('paper', 'simulation'));

CREATE INDEX IF NOT EXISTS trade_conditions_simulation_watching_idx
    ON trade_conditions (simulation_run_id, simulation_submitted_sequence, id)
    WHERE execution_mode = 'simulation' AND status = 'watching';

CREATE TABLE IF NOT EXISTS simulation_matcher_checkpoints (
    matcher_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sequence BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (sequence >= 0)
);
