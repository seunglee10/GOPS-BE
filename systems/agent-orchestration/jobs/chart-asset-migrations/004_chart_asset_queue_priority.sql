ALTER TABLE chart_assets.geometry_build_jobs
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS request_fingerprint TEXT;

UPDATE chart_assets.geometry_build_jobs
SET source = 'scheduled', priority = 10
WHERE job_id LIKE 'cab-scheduled-%';

UPDATE chart_assets.geometry_build_jobs
SET request_fingerprint = 'legacy:' || job_id
WHERE request_fingerprint IS NULL;

ALTER TABLE chart_assets.geometry_build_jobs
    ALTER COLUMN request_fingerprint SET NOT NULL;

ALTER TABLE chart_assets.geometry_build_jobs
    DROP CONSTRAINT IF EXISTS geometry_build_jobs_source_check;
ALTER TABLE chart_assets.geometry_build_jobs
    ADD CONSTRAINT geometry_build_jobs_source_check
    CHECK (source IN ('manual', 'scheduled'));

ALTER TABLE chart_assets.geometry_build_jobs
    DROP CONSTRAINT IF EXISTS geometry_build_jobs_priority_check;
ALTER TABLE chart_assets.geometry_build_jobs
    ADD CONSTRAINT geometry_build_jobs_priority_check
    CHECK (priority BETWEEN 0 AND 100);

CREATE UNIQUE INDEX IF NOT EXISTS geometry_build_jobs_active_request_idx
    ON chart_assets.geometry_build_jobs (request_fingerprint)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS geometry_build_jobs_priority_idx
    ON chart_assets.geometry_build_jobs (priority DESC, submitted_at, job_id)
    WHERE status IN ('queued', 'running');
