CREATE TABLE IF NOT EXISTS chart_assets.geometry_asset_snapshots (
    dataset_id TEXT NOT NULL,
    snapshot_cutoff TIMESTAMPTZ NOT NULL,
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
    PRIMARY KEY (dataset_id, symbol, "interval"),
    CHECK (as_of <= snapshot_cutoff)
);

CREATE INDEX IF NOT EXISTS geometry_asset_snapshots_lookup_idx
    ON chart_assets.geometry_asset_snapshots (dataset_id, symbol, "interval", snapshot_cutoff);

ALTER TABLE chart_assets.geometry_build_jobs
    ADD COLUMN IF NOT EXISTS build_target TEXT NOT NULL DEFAULT 'live',
    ADD COLUMN IF NOT EXISTS simulation_dataset_id TEXT,
    ADD COLUMN IF NOT EXISTS simulation_cutoff TIMESTAMPTZ;

ALTER TABLE chart_assets.geometry_build_jobs
    DROP CONSTRAINT IF EXISTS geometry_build_jobs_build_target_check;
ALTER TABLE chart_assets.geometry_build_jobs
    ADD CONSTRAINT geometry_build_jobs_build_target_check
    CHECK (build_target IN ('live', 'simulation'));

ALTER TABLE chart_assets.geometry_build_jobs
    DROP CONSTRAINT IF EXISTS geometry_build_jobs_simulation_context_check;
ALTER TABLE chart_assets.geometry_build_jobs
    ADD CONSTRAINT geometry_build_jobs_simulation_context_check
    CHECK (
        (build_target = 'live' AND simulation_dataset_id IS NULL AND simulation_cutoff IS NULL)
        OR
        (build_target = 'simulation' AND simulation_dataset_id IS NOT NULL AND simulation_cutoff IS NOT NULL)
    );
