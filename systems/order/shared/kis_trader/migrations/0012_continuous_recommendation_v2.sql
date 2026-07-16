CREATE TABLE IF NOT EXISTS order_coach_fill_history (
    id BIGSERIAL PRIMARY KEY,
    fill_id TEXT NOT NULL,
    observation_version BIGINT NOT NULL,
    user_sub TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    source_execution_id TEXT REFERENCES executions(execution_id),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    cumulative_filled_qty NUMERIC(24, 8) NOT NULL,
    average_fill_price NUMERIC(24, 8) NOT NULL,
    status TEXT NOT NULL,
    decision_at TIMESTAMPTZ NOT NULL,
    filled_at TIMESTAMPTZ NOT NULL,
    source_observed_at TIMESTAMPTZ NOT NULL,
    source_payload_digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT order_coach_fill_history_side_check CHECK (side IN ('buy', 'sell')),
    CONSTRAINT order_coach_fill_history_qty_check CHECK (cumulative_filled_qty > 0),
    CONSTRAINT order_coach_fill_history_price_check CHECK (average_fill_price > 0),
    CONSTRAINT order_coach_fill_history_version_unique UNIQUE (fill_id, observation_version),
    CONSTRAINT order_coach_fill_history_cumulative_unique UNIQUE (fill_id, cumulative_filled_qty)
);

CREATE INDEX IF NOT EXISTS order_coach_fill_history_user_filled_idx
    ON order_coach_fill_history (user_sub, filled_at, id);

CREATE INDEX IF NOT EXISTS order_coach_fill_history_order_idx
    ON order_coach_fill_history (order_id, observation_version DESC);

WITH historical_candidates AS (
    SELECT DISTINCT ON (
        e.order_id,
        COALESCE(
            e.payload->>'cumulative_filled_qty',
            e.payload->>'cumulativeFilledQty',
            e.payload->>'filled_qty',
            e.payload->>'filledQty'
        )
    )
        e.execution_id,
        e.order_id,
        e.payload,
        e.created_at,
        o.user_sub,
        upper(o.symbol) AS symbol,
        lower(o.side) AS side,
        COALESCE(
            e.payload->>'cumulative_filled_qty',
            e.payload->>'cumulativeFilledQty',
            e.payload->>'filled_qty',
            e.payload->>'filledQty'
        ) AS quantity_text,
        COALESCE(
            e.payload->>'average_fill_price',
            e.payload->>'averageFillPrice',
            e.payload->>'avg_fill_price',
            e.payload->>'fill_price',
            e.payload->>'execution_price',
            e.payload->>'price'
        ) AS price_text,
        upper(COALESCE(e.payload->>'status', o.status)) AS status
    FROM executions e
    JOIN orders o ON o.order_id = e.order_id
    WHERE o.user_sub IS NOT NULL
      AND lower(o.side) IN ('buy', 'sell')
    ORDER BY e.order_id, quantity_text, e.created_at, e.execution_id
), historical_valid AS (
    SELECT *,
           row_number() OVER (PARTITION BY order_id ORDER BY created_at, execution_id) AS observation_version
    FROM historical_candidates
    WHERE quantity_text ~ '^[0-9]+([.][0-9]+)?$'
      AND price_text ~ '^[0-9]+([.][0-9]+)?$'
      AND quantity_text::numeric > 0
      AND price_text::numeric > 0
)
INSERT INTO order_coach_fill_history (
    fill_id, observation_version, user_sub, order_id, source_execution_id,
    symbol, side, cumulative_filled_qty, average_fill_price, status,
    decision_at, filled_at, source_observed_at, source_payload_digest
)
SELECT
    'kis:' || order_id,
    observation_version,
    user_sub,
    order_id,
    execution_id,
    symbol,
    side,
    quantity_text::numeric,
    price_text::numeric,
    status,
    created_at,
    created_at,
    created_at,
    md5(payload::text)
FROM historical_valid
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS user_recommendation_preference_states (
    id BIGSERIAL PRIMARY KEY,
    user_sub TEXT NOT NULL,
    state_version BIGINT NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    event_cutoff TIMESTAMPTZ NOT NULL,
    prior_style TEXT NOT NULL,
    prior_weights JSONB NOT NULL,
    long_term_logits JSONB NOT NULL,
    session_logits JSONB NOT NULL,
    effective_weights JSONB NOT NULL,
    long_sample_count NUMERIC(20, 8) NOT NULL DEFAULT 0,
    session_sample_count NUMERIC(20, 8) NOT NULL DEFAULT 0,
    preference_confidence NUMERIC(12, 8) NOT NULL,
    preference_model_version TEXT NOT NULL,
    factor_schema_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_recommendation_preference_state_unique UNIQUE (user_sub, state_version),
    CONSTRAINT user_recommendation_preference_confidence_check
        CHECK (preference_confidence >= 0 AND preference_confidence <= 1)
);

CREATE INDEX IF NOT EXISTS user_recommendation_preference_states_latest_idx
    ON user_recommendation_preference_states (user_sub, state_version DESC);

CREATE TABLE IF NOT EXISTS user_recommendation_risk_states (
    id BIGSERIAL PRIMARY KEY,
    user_sub TEXT NOT NULL,
    state_version BIGINT NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    preset JSONB NOT NULL,
    observed_risk JSONB NOT NULL,
    effective_budget JSONB NOT NULL,
    evidence_status JSONB NOT NULL,
    data_ranges JSONB NOT NULL,
    risk_policy_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_recommendation_risk_state_unique UNIQUE (user_sub, state_version)
);

CREATE INDEX IF NOT EXISTS user_recommendation_risk_states_latest_idx
    ON user_recommendation_risk_states (user_sub, state_version DESC);

ALTER TABLE stock_recommendation_runs
    ADD COLUMN IF NOT EXISTS algorithm_version TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS preference_state_id BIGINT
        REFERENCES user_recommendation_preference_states(id),
    ADD COLUMN IF NOT EXISTS risk_state_id BIGINT
        REFERENCES user_recommendation_risk_states(id),
    ADD COLUMN IF NOT EXISTS fundamental_snapshot_provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS v2_input_digest TEXT;

CREATE TABLE IF NOT EXISTS stock_recommendation_candidate_features (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES stock_recommendation_runs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL,
    market_factor_scores JSONB NOT NULL,
    fundamental_factor_scores JSONB NOT NULL DEFAULT '{}'::jsonb,
    available_factor_scores JSONB NOT NULL,
    candidate_mean_scores JSONB NOT NULL,
    base_alpha_score NUMERIC(12, 8) NOT NULL,
    fundamental_score NUMERIC(12, 8),
    fundamental_weight NUMERIC(12, 8) NOT NULL DEFAULT 0,
    fundamental_status TEXT NOT NULL,
    feature_snapshot_id TEXT,
    feature_schema_version TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stock_recommendation_candidate_features_unique UNIQUE (run_id, symbol),
    CONSTRAINT stock_recommendation_candidate_fundamental_weight_check
        CHECK (fundamental_weight >= 0 AND fundamental_weight <= 0.15)
);

CREATE INDEX IF NOT EXISTS stock_recommendation_candidate_features_lookup_idx
    ON stock_recommendation_candidate_features (symbol, evaluated_at DESC, run_id DESC);

CREATE TABLE IF NOT EXISTS user_recommendation_preference_events (
    id BIGSERIAL PRIMARY KEY,
    fill_history_id BIGINT NOT NULL UNIQUE REFERENCES order_coach_fill_history(id),
    user_sub TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    decision_at TIMESTAMPTZ NOT NULL,
    candidate_run_id BIGINT REFERENCES stock_recommendation_runs(id),
    candidate_feature_id BIGINT REFERENCES stock_recommendation_candidate_features(id),
    event_status TEXT NOT NULL,
    skip_reason TEXT,
    relative_exposure JSONB NOT NULL DEFAULT '{}'::jsonb,
    event_strength NUMERIC(12, 8) NOT NULL DEFAULT 0,
    incremental_notional NUMERIC(24, 8),
    portfolio_equity NUMERIC(24, 8),
    order_cumulative_strength NUMERIC(12, 8) NOT NULL DEFAULT 0,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    event_schema_version TEXT NOT NULL,
    processing_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_recommendation_preference_event_status_check
        CHECK (event_status IN ('applied', 'skipped')),
    CONSTRAINT user_recommendation_preference_event_strength_check
        CHECK (event_strength >= 0 AND event_strength <= 1),
    CONSTRAINT user_recommendation_preference_order_strength_check
        CHECK (order_cumulative_strength >= 0 AND order_cumulative_strength <= 1)
);

CREATE INDEX IF NOT EXISTS user_recommendation_preference_events_user_idx
    ON user_recommendation_preference_events (user_sub, decision_at, id);
