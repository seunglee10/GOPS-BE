-- Run as the database owner. Login role creation and secret rotation stay in the
-- platform/IAM layer; these are privilege group roles only.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gops_api') THEN
        CREATE ROLE gops_api NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gops_worker') THEN
        CREATE ROLE gops_worker NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gops_migration') THEN
        CREATE ROLE gops_migration NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO gops_api, gops_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO gops_api, gops_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO gops_api, gops_worker;
GRANT ALL PRIVILEGES ON SCHEMA public TO gops_migration;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO gops_migration;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO gops_migration;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO gops_api, gops_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO gops_api, gops_worker;
