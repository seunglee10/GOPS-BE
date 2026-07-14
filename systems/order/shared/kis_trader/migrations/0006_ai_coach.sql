ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS user_sub TEXT;

CREATE INDEX IF NOT EXISTS idx_orders_user_sub_occurred_at
    ON orders (user_sub, occurred_at DESC)
    WHERE user_sub IS NOT NULL;

CREATE TABLE IF NOT EXISTS user_portfolio_snapshot_history (
    id BIGSERIAL PRIMARY KEY,
    user_sub TEXT NOT NULL,
    payload JSONB NOT NULL,
    source_as_of TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS user_portfolio_snapshot_history_user_asof_idx
    ON user_portfolio_snapshot_history (user_sub, source_as_of DESC);

CREATE TABLE IF NOT EXISTS trade_decision_check_events (
    id BIGSERIAL PRIMARY KEY,
    user_sub TEXT NOT NULL,
    fill_id TEXT NOT NULL,
    category TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    source TEXT,
    source_as_of TIMESTAMPTZ,
    checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT trade_decision_check_events_category_check
        CHECK (category IN ('chart', 'news', 'fundamentals', 'market')),
    CONSTRAINT trade_decision_check_events_status_check
        CHECK (status IN ('checked', 'unchecked', 'insufficient_data', 'not_applicable'))
);

CREATE INDEX IF NOT EXISTS trade_decision_check_events_user_fill_idx
    ON trade_decision_check_events (user_sub, fill_id, created_at);
