CREATE SCHEMA IF NOT EXISTS chart_assets;

CREATE TABLE IF NOT EXISTS chart_assets.geometry_assets (
    symbol TEXT NOT NULL,
    "interval" TEXT NOT NULL CHECK ("interval" IN ('1m', '5m', '10m', '1h', '4h', '1D', '1W')),
    as_of TIMESTAMPTZ NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    asset_version TEXT NOT NULL CHECK (asset_version = 'geometry'),
    algorithm_version TEXT NOT NULL,
    status TEXT NOT NULL,
    coverage_state TEXT NOT NULL CHECK (coverage_state IN ('full', 'partial')),
    drawing_count SMALLINT NOT NULL CHECK (drawing_count BETWEEN 0 AND 8),
    payload_bytes INTEGER NOT NULL CHECK (payload_bytes >= 0),
    input_digest TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, "interval")
);

CREATE TABLE IF NOT EXISTS chart_assets.geometry_build_jobs (
    job_id TEXT PRIMARY KEY,
    requested_by TEXT NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'completed_with_warnings', 'completed_with_errors', 'failed', 'canceled')),
    force_build BOOLEAN NOT NULL DEFAULT false,
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'scheduled')),
    priority SMALLINT NOT NULL DEFAULT 100 CHECK (priority BETWEEN 0 AND 100),
    request_fingerprint TEXT NOT NULL,
    requested_intervals JSONB NOT NULL,
    symbol_count INTEGER NOT NULL CHECK (symbol_count > 0),
    total_items INTEGER NOT NULL CHECK (total_items > 0),
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    repair JSONB NOT NULL DEFAULT '{}'::jsonb,
    logs JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_entities INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NULL,
    finished_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chart_assets.geometry_build_items (
    job_id TEXT NOT NULL REFERENCES chart_assets.geometry_build_jobs(job_id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    "interval" TEXT NOT NULL CHECK ("interval" IN ('1m', '5m', '10m', '1h', '4h', '1D', '1W')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'saved', 'saved_with_warning', 'unchanged', 'failed', 'skipped')),
    stage TEXT NOT NULL DEFAULT 'queued',
    attempts SMALLINT NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 2),
    worker_id TEXT NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    error TEXT NULL,
    warning TEXT NULL,
    reason TEXT NULL,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    created_entities SMALLINT NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NULL,
    finished_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, symbol, "interval")
);

CREATE INDEX IF NOT EXISTS geometry_build_items_claim_idx
    ON chart_assets.geometry_build_items (status, lease_expires_at, job_id)
    WHERE status IN ('pending', 'running');

CREATE UNIQUE INDEX IF NOT EXISTS geometry_build_jobs_active_request_idx
    ON chart_assets.geometry_build_jobs (request_fingerprint)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS geometry_build_jobs_priority_idx
    ON chart_assets.geometry_build_jobs (priority DESC, submitted_at, job_id)
    WHERE status IN ('queued', 'running');

ALTER TABLE chart_assets.geometry_assets
    DROP CONSTRAINT IF EXISTS geometry_assets_drawing_count_check;
ALTER TABLE chart_assets.geometry_assets
    ADD CONSTRAINT geometry_assets_drawing_count_check
    CHECK (drawing_count BETWEEN 0 AND 8);
