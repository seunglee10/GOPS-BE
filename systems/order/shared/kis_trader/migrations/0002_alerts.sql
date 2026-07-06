CREATE TABLE IF NOT EXISTS alerts (
    id BIGSERIAL PRIMARY KEY,
    user_sub TEXT NOT NULL,
    symbol TEXT NOT NULL,
    type TEXT NOT NULL,
    direction TEXT,
    target_price NUMERIC(18,4),
    change_pct NUMERIC(6,2),
    window_min INTEGER,
    repeat BOOLEAN NOT NULL DEFAULT false,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    CONSTRAINT alerts_type_check CHECK (type IN ('price_cross', 'spike')),
    CONSTRAINT alerts_direction_check CHECK (direction IS NULL OR direction IN ('above', 'below')),
    CONSTRAINT alerts_status_check CHECK (status IN ('active', 'fired', 'disabled', 'expired')),
    CONSTRAINT alerts_price_cross_shape CHECK (
        type <> 'price_cross'
        OR (target_price IS NOT NULL AND direction IS NOT NULL)
    ),
    CONSTRAINT alerts_spike_shape CHECK (
        type <> 'spike'
        OR (change_pct IS NOT NULL AND window_min IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS alerts_active_user_idx ON alerts (user_sub) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS alerts_active_symbol_idx ON alerts (symbol, type) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS notifications (
    id BIGSERIAL PRIMARY KEY,
    user_sub TEXT NOT NULL,
    alert_id BIGINT REFERENCES alerts(id) ON DELETE SET NULL,
    event_id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS notifications_user_id_desc_idx ON notifications (user_sub, id DESC);
CREATE INDEX IF NOT EXISTS notifications_user_unread_idx ON notifications (user_sub) WHERE read_at IS NULL;
