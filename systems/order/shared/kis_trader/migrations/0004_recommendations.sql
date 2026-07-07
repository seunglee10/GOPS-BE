CREATE TABLE IF NOT EXISTS user_investment_profiles (
    user_sub TEXT PRIMARY KEY,
    risk_level TEXT NOT NULL,
    horizon TEXT NOT NULL DEFAULT 'intraday',
    max_drawdown_pct NUMERIC(6,2) NOT NULL,
    preferred_sectors JSONB NOT NULL DEFAULT '[]'::jsonb,
    excluded_sectors JSONB NOT NULL DEFAULT '[]'::jsonb,
    excluded_symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT investment_profiles_risk_check CHECK (risk_level IN ('conservative', 'balanced', 'aggressive')),
    CONSTRAINT investment_profiles_horizon_check CHECK (horizon IN ('intraday')),
    CONSTRAINT investment_profiles_drawdown_check CHECK (max_drawdown_pct > 0 AND max_drawdown_pct <= 50)
);

CREATE TABLE IF NOT EXISTS stock_recommendation_runs (
    id BIGSERIAL PRIMARY KEY,
    user_sub TEXT NOT NULL,
    run_key TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    market_date TEXT NOT NULL,
    status TEXT NOT NULL,
    profile_snapshot JSONB NOT NULL,
    market_snapshot_time TEXT NOT NULL,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stock_recommendation_runs_status_check CHECK (
        status IN ('completed', 'empty', 'market_closed', 'profile_required', 'failed')
    ),
    CONSTRAINT stock_recommendation_runs_user_key_unique UNIQUE (user_sub, run_key)
);

CREATE INDEX IF NOT EXISTS stock_recommendation_runs_user_generated_idx
    ON stock_recommendation_runs (user_sub, generated_at DESC);

CREATE TABLE IF NOT EXISTS stock_recommendation_items (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES stock_recommendation_runs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'buy',
    rank INTEGER NOT NULL,
    score NUMERIC(6,2) NOT NULL,
    confidence NUMERIC(5,4) NOT NULL,
    sector TEXT,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    risk_warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    metrics_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stock_recommendation_items_action_check CHECK (action IN ('buy')),
    CONSTRAINT stock_recommendation_items_rank_check CHECK (rank >= 1),
    CONSTRAINT stock_recommendation_items_score_check CHECK (score >= 0 AND score <= 100),
    CONSTRAINT stock_recommendation_items_confidence_check CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE INDEX IF NOT EXISTS stock_recommendation_items_run_rank_idx
    ON stock_recommendation_items (run_id, rank);

CREATE TABLE IF NOT EXISTS user_portfolio_snapshots (
    user_sub TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS user_portfolio_snapshots_updated_idx
    ON user_portfolio_snapshots (updated_at DESC);
