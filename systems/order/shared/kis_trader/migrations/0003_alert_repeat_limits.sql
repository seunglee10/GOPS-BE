ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS repeat_limit INTEGER;

ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS triggered_count INTEGER NOT NULL DEFAULT 0;

UPDATE alerts
SET repeat_limit = CASE WHEN repeat THEN NULL ELSE 1 END
WHERE repeat_limit IS NULL;

DO $$
BEGIN
    ALTER TABLE alerts
        ADD CONSTRAINT alerts_repeat_limit_check CHECK (repeat_limit IS NULL OR repeat_limit >= 1);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE alerts
        ADD CONSTRAINT alerts_triggered_count_check CHECK (triggered_count >= 0);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
