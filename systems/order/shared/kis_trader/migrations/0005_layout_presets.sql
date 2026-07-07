CREATE TABLE IF NOT EXISTS user_layout_presets (
    user_sub TEXT PRIMARY KEY,
    presets JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
