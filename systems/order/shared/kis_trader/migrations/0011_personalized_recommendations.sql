ALTER TABLE user_investment_profiles
    ADD COLUMN IF NOT EXISTS recommendation_style TEXT NOT NULL DEFAULT 'balanced';

ALTER TABLE user_investment_profiles
    DROP CONSTRAINT IF EXISTS investment_profiles_recommendation_style_check;

ALTER TABLE user_investment_profiles
    ADD CONSTRAINT investment_profiles_recommendation_style_check
    CHECK (recommendation_style IN ('momentum', 'balanced', 'stable'));

ALTER TABLE stock_recommendation_runs
    ADD COLUMN IF NOT EXISTS portfolio_snapshot_history_id BIGINT
        REFERENCES user_portfolio_snapshot_history(id),
    ADD COLUMN IF NOT EXISTS weights_version TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS personalization_input_digest TEXT,
    ADD COLUMN IF NOT EXISTS personalization_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS stock_recommendation_runs_portfolio_history_idx
    ON stock_recommendation_runs (portfolio_snapshot_history_id)
    WHERE portfolio_snapshot_history_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS stock_recommendation_model_registry (
    model_version TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    training_cutoff TIMESTAMPTZ NOT NULL,
    feature_definitions JSONB NOT NULL,
    weights JSONB NOT NULL,
    validation_report JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    CONSTRAINT stock_recommendation_model_registry_status_check
        CHECK (status IN ('draft', 'approved', 'active', 'retired'))
);

CREATE UNIQUE INDEX IF NOT EXISTS stock_recommendation_model_registry_one_active_idx
    ON stock_recommendation_model_registry ((status))
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS stock_recommendation_outcomes (
    id BIGSERIAL PRIMARY KEY,
    recommendation_item_id BIGINT NOT NULL UNIQUE
        REFERENCES stock_recommendation_items(id) ON DELETE CASCADE,
    label_market_date DATE NOT NULL,
    symbol_open NUMERIC(20,8) NOT NULL,
    symbol_close NUMERIC(20,8) NOT NULL,
    spy_open NUMERIC(20,8) NOT NULL,
    spy_close NUMERIC(20,8) NOT NULL,
    open_to_close_excess_return_pct NUMERIC(12,8) NOT NULL,
    label_version TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS stock_recommendation_outcomes_market_date_idx
    ON stock_recommendation_outcomes (label_market_date, recommendation_item_id);
