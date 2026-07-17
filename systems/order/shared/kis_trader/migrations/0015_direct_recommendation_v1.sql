CREATE TABLE IF NOT EXISTS user_investment_profile_history (
    id BIGSERIAL PRIMARY KEY,
    user_sub TEXT NOT NULL,
    payload JSONB NOT NULL,
    source_as_of TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS user_investment_profile_history_lookup_idx
    ON user_investment_profile_history (user_sub, source_as_of DESC, id DESC);

INSERT INTO user_investment_profile_history (user_sub, payload, source_as_of)
SELECT
    user_sub,
    jsonb_build_object(
        'risk_level', risk_level,
        'recommendation_style', COALESCE(recommendation_style, 'balanced'),
        'horizon', horizon,
        'max_drawdown_pct', max_drawdown_pct,
        'preferred_sectors', preferred_sectors,
        'excluded_sectors', excluded_sectors,
        'excluded_symbols', excluded_symbols
    ),
    updated_at
FROM user_investment_profiles p
WHERE NOT EXISTS (
    SELECT 1
    FROM user_investment_profile_history h
    WHERE h.user_sub = p.user_sub AND h.source_as_of = p.updated_at
);

ALTER TABLE stock_recommendation_items
    DROP CONSTRAINT IF EXISTS stock_recommendation_items_action_check;

ALTER TABLE stock_recommendation_items
    ADD CONSTRAINT stock_recommendation_items_action_check
        CHECK (action IN ('buy', 'conditional_buy', 'watch', 'not_suitable')),
    ADD COLUMN IF NOT EXISTS decision_json JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN stock_recommendation_items.decision_json IS
    'Deterministic direct-recommendation decision, entry routes, invalidation, targets, sizing and evidence contract.';
