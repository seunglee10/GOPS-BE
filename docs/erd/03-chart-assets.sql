-- ERDCloud/MySQL display DDL. Actual PostgreSQL tables live in chart_assets.*.
CREATE TABLE chart_assets_analysis_assets (
  symbol VARCHAR(64) NOT NULL, `interval` VARCHAR(16) NOT NULL,
  as_of DATETIME(3) NOT NULL, asset_version VARCHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL, payload JSON NOT NULL,
  PRIMARY KEY (symbol, `interval`)
);
CREATE TABLE chart_assets_geometry_assets (
  symbol VARCHAR(64) NOT NULL, `interval` VARCHAR(16) NOT NULL,
  as_of DATETIME(3) NOT NULL, algorithm_version VARCHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL, payload JSON NOT NULL,
  PRIMARY KEY (symbol, `interval`)
);
CREATE TABLE chart_assets_geometry_build_jobs (
  job_id VARCHAR(128) PRIMARY KEY, requested_by VARCHAR(255) NOT NULL,
  status VARCHAR(32) NOT NULL, submitted_at DATETIME(3) NOT NULL,
  requested_intervals JSON NOT NULL, symbol_count INT NOT NULL
);
CREATE TABLE chart_assets_geometry_build_items (
  job_id VARCHAR(128) NOT NULL, symbol VARCHAR(64) NOT NULL,
  `interval` VARCHAR(16) NOT NULL, status VARCHAR(32) NOT NULL,
  worker_id VARCHAR(128), lease_expires_at DATETIME(3),
  PRIMARY KEY (job_id, symbol, `interval`),
  CONSTRAINT fk_geometry_item_job FOREIGN KEY (job_id)
    REFERENCES chart_assets_geometry_build_jobs(job_id)
);
CREATE TABLE chart_assets_geometry_asset_snapshots (
  dataset_id VARCHAR(128) NOT NULL, symbol VARCHAR(64) NOT NULL,
  `interval` VARCHAR(16) NOT NULL, snapshot_cutoff DATETIME(3) NOT NULL,
  payload JSON NOT NULL, PRIMARY KEY (dataset_id, symbol, `interval`)
);
