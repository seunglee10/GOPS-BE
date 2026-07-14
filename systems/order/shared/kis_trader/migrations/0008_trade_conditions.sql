ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS trade_conditions (
    id BIGSERIAL PRIMARY KEY,
    user_sub TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    proposal_id TEXT,
    analysis_id TEXT,
    alert_id BIGINT NOT NULL UNIQUE REFERENCES alerts(id) ON DELETE CASCADE,
    side TEXT NOT NULL,
    limit_price NUMERIC(18,4) NOT NULL,
    quantity NUMERIC(18,4) NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'NASD',
    execution_enabled BOOLEAN NOT NULL DEFAULT true,
    status TEXT NOT NULL DEFAULT 'watching',
    validity TEXT NOT NULL DEFAULT 'DAY',
    market_hours TEXT NOT NULL DEFAULT 'REGULAR',
    trigger_event_id TEXT,
    triggered_at TIMESTAMPTZ,
    order_id TEXT,
    error_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    CONSTRAINT trade_conditions_source_check CHECK (source IN ('manual', 'agent')),
    CONSTRAINT trade_conditions_side_check CHECK (side IN ('buy', 'sell')),
    CONSTRAINT trade_conditions_status_check CHECK (
        status IN ('watching', 'paused', 'triggered', 'executing', 'completed', 'blocked', 'failed', 'expired', 'canceled')
    ),
    CONSTRAINT trade_conditions_quantity_check CHECK (quantity > 0 AND quantity = trunc(quantity)),
    CONSTRAINT trade_conditions_limit_price_check CHECK (limit_price > 0),
    CONSTRAINT trade_conditions_version_check CHECK (version > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS trade_conditions_user_proposal_unique
    ON trade_conditions (user_sub, proposal_id)
    WHERE proposal_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS trade_conditions_trigger_event_unique
    ON trade_conditions (trigger_event_id)
    WHERE trigger_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS trade_conditions_user_created_idx
    ON trade_conditions (user_sub, created_at DESC);
CREATE INDEX IF NOT EXISTS trade_conditions_watching_idx
    ON trade_conditions (status, expires_at)
    WHERE status = 'watching';
