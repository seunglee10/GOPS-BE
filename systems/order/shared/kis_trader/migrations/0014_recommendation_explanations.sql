ALTER TABLE stock_recommendation_items
    ADD COLUMN IF NOT EXISTS explanation_json JSONB;

COMMENT ON COLUMN stock_recommendation_items.explanation_json IS
    'Versioned Korean V3 explanation payload; nullable for legacy and historical rows.';
