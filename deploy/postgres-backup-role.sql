\set ON_ERROR_STOP on

-- Run as a PostgreSQL administrator while connected to the application DB.
-- The role owns nothing and receives only the predefined read-data role.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'before_after_backup') THEN
        CREATE ROLE before_after_backup LOGIN INHERIT;
    END IF;
END
$$;

ALTER ROLE before_after_backup
    LOGIN INHERIT
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE :"database_name" TO before_after_backup;
GRANT pg_read_all_data TO before_after_backup;
GRANT USAGE ON SCHEMA public TO before_after_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO before_after_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO before_after_backup;
REVOKE CREATE ON SCHEMA public FROM before_after_backup;
REVOKE TEMPORARY ON DATABASE :"database_name" FROM before_after_backup;
