ALTER TABLE stock_recommendation_evidence_candidates
    ADD COLUMN IF NOT EXISTS narrative_context JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN stock_recommendation_evidence_candidates.narrative_context IS
    'Immutable recommendation-narrative-context.v1 company, catalyst and provenance evidence at the snapshot cutoff.';
