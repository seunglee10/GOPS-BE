CREATE TABLE IF NOT EXISTS stock_recommendation_evidence_snapshots (
    id BIGSERIAL PRIMARY KEY,
    snapshot_key TEXT NOT NULL UNIQUE,
    slot_start TIMESTAMPTZ NOT NULL,
    market_date DATE NOT NULL,
    session_mode TEXT NOT NULL,
    cutoff TIMESTAMPTZ NOT NULL,
    universe JSONB NOT NULL,
    rule_set_version TEXT NOT NULL,
    source_digests JSONB NOT NULL,
    source_status JSONB NOT NULL,
    status TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stock_recommendation_evidence_snapshot_session_check
        CHECK (session_mode IN ('pre', 'regular')),
    CONSTRAINT stock_recommendation_evidence_snapshot_status_check
        CHECK (status IN ('completed', 'empty'))
);

CREATE INDEX IF NOT EXISTS stock_recommendation_evidence_snapshots_slot_idx
    ON stock_recommendation_evidence_snapshots (slot_start, session_mode, rule_set_version);

CREATE TABLE IF NOT EXISTS stock_recommendation_evidence_candidates (
    id BIGSERIAL PRIMARY KEY,
    snapshot_id BIGINT NOT NULL REFERENCES stock_recommendation_evidence_snapshots(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    sector TEXT NOT NULL,
    industry TEXT NOT NULL,
    change_percent NUMERIC(12, 6),
    raw_factors JSONB NOT NULL,
    normalized_factors JSONB NOT NULL,
    block_scores JSONB NOT NULL,
    base_setup_score NUMERIC(12, 8) NOT NULL,
    evidence_reliability NUMERIC(12, 8) NOT NULL,
    reliability_components JSONB NOT NULL,
    rejection_reasons JSONB NOT NULL,
    daily_returns_60 JSONB NOT NULL,
    market_item JSONB NOT NULL,
    input_digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stock_recommendation_evidence_candidate_unique UNIQUE (snapshot_id, symbol),
    CONSTRAINT stock_recommendation_evidence_reliability_check
        CHECK (evidence_reliability >= 0 AND evidence_reliability <= 100)
);

CREATE INDEX IF NOT EXISTS stock_recommendation_evidence_candidates_lookup_idx
    ON stock_recommendation_evidence_candidates (snapshot_id, evidence_reliability DESC, symbol);

ALTER TABLE stock_recommendation_runs
    ADD COLUMN IF NOT EXISTS evidence_snapshot_id BIGINT
        REFERENCES stock_recommendation_evidence_snapshots(id);

ALTER TABLE user_recommendation_preference_events
    ADD COLUMN IF NOT EXISTS evidence_candidate_id BIGINT
        REFERENCES stock_recommendation_evidence_candidates(id);
