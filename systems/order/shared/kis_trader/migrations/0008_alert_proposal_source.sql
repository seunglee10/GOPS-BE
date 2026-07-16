ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS proposal_source TEXT;

DO $$
BEGIN
    ALTER TABLE alerts
        ADD CONSTRAINT alerts_proposal_source_check CHECK (
            proposal_source IS NULL
            OR proposal_source IN ('daily_trade', 'entry_habit', 'exit_habit', 'portfolio_risk')
        );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
