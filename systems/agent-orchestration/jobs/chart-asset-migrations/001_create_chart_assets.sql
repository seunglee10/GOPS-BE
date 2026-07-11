CREATE SCHEMA IF NOT EXISTS chart_assets;

CREATE TABLE IF NOT EXISTS chart_assets.analysis_assets (
    symbol TEXT NOT NULL,
    "interval" TEXT NOT NULL CONSTRAINT analysis_assets_interval_check
        CHECK ("interval" IN ('1m', '5m', '10m', '1h', '4h', '1D', '1W', '1M')),
    as_of TIMESTAMPTZ NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    asset_version TEXT NOT NULL,
    kernel_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    quality_state TEXT NULL,
    drawing_count SMALLINT NOT NULL CHECK (drawing_count >= 0),
    payload_bytes INTEGER NOT NULL CHECK (payload_bytes >= 0),
    asset_content_digest TEXT NULL,
    payload_digest TEXT NOT NULL,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, "interval")
);
