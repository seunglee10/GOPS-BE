CREATE TABLE IF NOT EXISTS user_recommendation_score_profiles (
    id BIGSERIAL PRIMARY KEY,
    user_sub TEXT NOT NULL,
    name TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    block_weights JSONB NOT NULL,
    factor_weights JSONB NOT NULL,
    portfolio_weight NUMERIC(6,2) NOT NULL,
    portfolio_factor_weights JSONB NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT recommendation_score_profile_name_length CHECK (char_length(name) BETWEEN 1 AND 40),
    CONSTRAINT recommendation_score_profile_portfolio_weight CHECK (portfolio_weight BETWEEN 0 AND 100)
);

CREATE UNIQUE INDEX IF NOT EXISTS user_recommendation_score_profiles_name_idx
    ON user_recommendation_score_profiles (user_sub, lower(name));

ALTER TABLE user_investment_profiles
    ADD COLUMN IF NOT EXISTS active_score_profile_id BIGINT
        REFERENCES user_recommendation_score_profiles(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS profile_revision BIGINT NOT NULL DEFAULT 1;

ALTER TABLE stock_recommendation_runs
    ADD COLUMN IF NOT EXISTS scoring_input_digest TEXT,
    ADD COLUMN IF NOT EXISTS scoring_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE stock_recommendation_runs
    DROP CONSTRAINT IF EXISTS stock_recommendation_runs_preference_state_id_fkey,
    DROP CONSTRAINT IF EXISTS stock_recommendation_runs_risk_state_id_fkey;

ALTER TABLE stock_recommendation_runs
    DROP COLUMN IF EXISTS preference_state_id,
    DROP COLUMN IF EXISTS risk_state_id,
    DROP COLUMN IF EXISTS v2_input_digest,
    DROP COLUMN IF EXISTS personalization_input_digest,
    DROP COLUMN IF EXISTS personalization_snapshot;

DROP TABLE IF EXISTS user_recommendation_preference_events;
DROP TABLE IF EXISTS stock_recommendation_candidate_features;
DROP TABLE IF EXISTS user_recommendation_preference_states;
DROP TABLE IF EXISTS user_recommendation_risk_states;

UPDATE stock_recommendation_runs
SET summary = summary - 'personalization'
WHERE summary ? 'personalization';

UPDATE stock_recommendation_items
SET metrics_snapshot = metrics_snapshot
    - 'preferenceFitScore'
    - 'preferenceConfidence'
    - 'preferenceWeight'
    - 'preferenceContribution'
    - 'personalizationDelta'
    - 'effectivePreferenceWeights'
    - 'personalScore'
    - 'personalization';
