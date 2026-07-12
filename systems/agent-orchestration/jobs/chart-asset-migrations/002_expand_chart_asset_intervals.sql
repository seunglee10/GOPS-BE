ALTER TABLE chart_assets.analysis_assets
    DROP CONSTRAINT IF EXISTS analysis_assets_interval_check;

ALTER TABLE chart_assets.analysis_assets
    ADD CONSTRAINT analysis_assets_interval_check
    CHECK ("interval" IN ('1m', '5m', '10m', '1h', '4h', '1D', '1W', '1M'));
