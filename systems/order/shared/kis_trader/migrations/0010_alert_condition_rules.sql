ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS condition_version SMALLINT NOT NULL DEFAULT 1;

ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS condition JSONB;

ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS created_via TEXT NOT NULL DEFAULT 'manual';

ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS request_id TEXT;

ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS last_triggered_at TIMESTAMPTZ;

UPDATE alerts
SET condition = CASE
    WHEN type = 'price_cross' THEN jsonb_strip_nulls(jsonb_build_object(
        'kind', 'price_cross',
        'operator', direction,
        'threshold', target_price
    ))
    WHEN type = 'spike' THEN jsonb_strip_nulls(jsonb_build_object(
        'kind', 'price_change',
        'operator', COALESCE(direction, 'either'),
        'threshold', change_pct,
        'windowMin', window_min
    ))
    ELSE condition
END
WHERE condition IS NULL;

UPDATE alerts
SET created_via = 'ai_coach'
WHERE proposal_source IS NOT NULL
  AND created_via = 'manual';

UPDATE alerts AS alert
SET created_via = 'trade_condition'
FROM trade_conditions AS trade_condition
WHERE trade_condition.alert_id = alert.id
  AND alert.created_via = 'manual';

ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_type_check;
ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_price_cross_shape;
ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_spike_shape;

DO $$
BEGIN
    ALTER TABLE alerts
        ADD CONSTRAINT alerts_type_check CHECK (
            type IN ('price_cross', 'spike', 'volume_absolute', 'volume_relative', 'rsi_threshold')
        );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE alerts
        ADD CONSTRAINT alerts_condition_version_check CHECK (condition_version = 1);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE alerts
        ADD CONSTRAINT alerts_condition_object_check CHECK (
            condition IS NOT NULL AND jsonb_typeof(condition) = 'object'
        );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE alerts
        ADD CONSTRAINT alerts_created_via_check CHECK (
            created_via IN ('manual', 'chart', 'ai_coach', 'agent_chat', 'trade_condition')
        );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS alerts_user_request_unique
    ON alerts (user_sub, request_id)
    WHERE request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS alerts_active_symbol_interval_idx
    ON alerts (symbol, type, ((condition ->> 'interval')))
    WHERE status = 'active';
