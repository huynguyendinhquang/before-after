\set ON_ERROR_STOP on

-- Per-database part of deploy/bootstrap-postgres-backup-role.sh.
-- psql variables: database_name, production_database, app_owner, is_target.
-- The role is deliberately NOINHERIT: all memberships are removed below.
SELECT set_config('before_after.app_owner', :'app_owner', false);
SELECT set_config('before_after.is_target', :'is_target', false);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'before_after_backup') THEN
        CREATE ROLE before_after_backup LOGIN NOINHERIT;
    END IF;
END
$$;

ALTER ROLE before_after_backup
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS
    NOINHERIT;

-- Remove every inherited membership. A membership that survives is a
-- provisioning failure, never an acceptable way to grant backup access.
DO $$
DECLARE
    membership record;
BEGIN
    FOR membership IN
        SELECT parent.rolname AS parent_name
        FROM pg_auth_members AS member
        JOIN pg_roles AS parent ON parent.oid = member.roleid
        JOIN pg_roles AS child ON child.oid = member.member
        WHERE child.rolname = 'before_after_backup'
    LOOP
        EXECUTE format('REVOKE %I FROM %I', membership.parent_name, 'before_after_backup');
    END LOOP;
    IF EXISTS (
        SELECT 1
        FROM pg_auth_members AS member
        JOIN pg_roles AS child ON child.oid = member.member
        WHERE child.rolname = 'before_after_backup'
    ) THEN
        RAISE EXCEPTION 'before_after_backup has a role membership that could not be revoked';
    END IF;
END
$$;

-- Ownership cannot be made read-only. Fail closed so an administrator must
-- transfer or drop every object owned by this role before provisioning again.
DO $$
DECLARE
    role_oid oid;
BEGIN
    SELECT oid INTO role_oid FROM pg_roles WHERE rolname = 'before_after_backup';
    IF EXISTS (
        SELECT 1
        FROM pg_shdepend
        WHERE refclassid = 'pg_authid'::regclass
          AND refobjid = role_oid
          AND deptype = 'o'
    ) THEN
        RAISE EXCEPTION 'before_after_backup owns an object; transfer ownership before provisioning';
    END IF;
END
$$;

-- Clear every explicit grant to the role in this database. PUBLIC CREATE and
-- TEMPORARY are removed too, so PUBLIC CONNECT cannot become a write/DDL path.
REVOKE ALL PRIVILEGES ON DATABASE :"database_name" FROM before_after_backup;
REVOKE TEMPORARY ON DATABASE :"database_name" FROM PUBLIC;

DO $$
DECLARE
    schema_name text;
BEGIN
    FOR schema_name IN
        SELECT nspname
        FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_temp_%'
          AND nspname NOT LIKE 'pg_toast_temp_%'
    LOOP
        EXECUTE format('REVOKE ALL PRIVILEGES ON SCHEMA %I FROM %I', schema_name, 'before_after_backup');
        EXECUTE format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I FROM %I', schema_name, 'before_after_backup');
        EXECUTE format('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I FROM %I', schema_name, 'before_after_backup');
        EXECUTE format('REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA %I FROM %I', schema_name, 'before_after_backup');
        EXECUTE format('REVOKE ALL PRIVILEGES ON ALL PROCEDURES IN SCHEMA %I FROM %I', schema_name, 'before_after_backup');
        EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM PUBLIC', schema_name);
    END LOOP;
END
$$;

-- Remove stale default-privilege grants regardless of which owner created them.
DO $$
DECLARE
    default_acl record;
    object_kind text;
    schema_clause text;
BEGIN
    FOR default_acl IN
        SELECT owner.rolname AS owner_name,
               namespace.nspname AS schema_name,
               privileges.defaclobjtype AS object_type
        FROM pg_default_acl AS privileges
        JOIN pg_roles AS owner ON owner.oid = privileges.defaclrole
        LEFT JOIN pg_namespace AS namespace ON namespace.oid = privileges.defaclnamespace
    LOOP
        object_kind := CASE default_acl.object_type
            WHEN 'r' THEN 'TABLES'
            WHEN 'S' THEN 'SEQUENCES'
            WHEN 'f' THEN 'FUNCTIONS'
            WHEN 'p' THEN 'PROCEDURES'
            WHEN 'T' THEN 'TYPES'
            ELSE NULL
        END;
        IF object_kind IS NULL THEN
            CONTINUE;
        END IF;
        schema_clause := CASE
            WHEN default_acl.schema_name IS NULL THEN ''
            ELSE format(' IN SCHEMA %I', default_acl.schema_name)
        END;
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I%s REVOKE ALL ON %s FROM %I',
            default_acl.owner_name,
            schema_clause,
            object_kind,
            'before_after_backup'
        );
    END LOOP;
END
$$;

-- Only the production application database receives backup access.
\if :is_target
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_roles
            WHERE rolname = current_setting('before_after.app_owner')
        ) THEN
            RAISE EXCEPTION 'configured app owner does not exist';
        END IF;
    END
    $$;
    GRANT CONNECT ON DATABASE :"database_name" TO before_after_backup;
    GRANT USAGE ON SCHEMA public TO before_after_backup;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO before_after_backup;
    GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO before_after_backup;
    ALTER DEFAULT PRIVILEGES FOR ROLE :"app_owner" IN SCHEMA public
        GRANT SELECT ON TABLES TO before_after_backup;
    ALTER DEFAULT PRIVILEGES FOR ROLE :"app_owner" IN SCHEMA public
        GRANT SELECT ON SEQUENCES TO before_after_backup;
\endif

-- Verify the effective boundary, including PUBLIC and any future stale ACL.
DO $$
DECLARE
    object_row record;
    schema_name text;
    privilege_name text;
    target boolean := current_setting('before_after.is_target')::boolean;
BEGIN
    FOR schema_name IN
        SELECT nspname
        FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_temp_%'
          AND nspname NOT LIKE 'pg_toast_temp_%'
          AND nspname NOT IN ('pg_catalog', 'information_schema')
    LOOP
        IF has_schema_privilege('before_after_backup', schema_name, 'CREATE') THEN
            RAISE EXCEPTION 'backup role can CREATE in schema %', schema_name;
        END IF;
    END LOOP;

    IF target THEN
        IF NOT has_schema_privilege('before_after_backup', 'public', 'USAGE') THEN
            RAISE EXCEPTION 'backup role cannot use the public schema in the production database';
        END IF;
        FOR object_row IN
            SELECT c.oid, n.nspname, c.relname
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
        LOOP
            IF NOT has_table_privilege('before_after_backup', object_row.oid, 'SELECT') THEN
                RAISE EXCEPTION 'backup role cannot SELECT %.%', object_row.nspname, object_row.relname;
            END IF;
            FOREACH privilege_name IN ARRAY ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']
            LOOP
                IF has_table_privilege('before_after_backup', object_row.oid, privilege_name) THEN
                    RAISE EXCEPTION 'backup role has unsafe % privilege on %.%', privilege_name, object_row.nspname, object_row.relname;
                END IF;
            END LOOP;
        END LOOP;
    ELSE
        FOR object_row IN
            SELECT c.oid, n.nspname, c.relname
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname NOT LIKE 'pg_temp_%'
              AND n.nspname NOT LIKE 'pg_toast_temp_%'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
        LOOP
            IF has_table_privilege('before_after_backup', object_row.oid, 'SELECT')
                OR has_table_privilege('before_after_backup', object_row.oid, 'INSERT')
                OR has_table_privilege('before_after_backup', object_row.oid, 'UPDATE')
                OR has_table_privilege('before_after_backup', object_row.oid, 'DELETE')
            THEN
                RAISE EXCEPTION 'backup role retains effective privileges in non-production database %', current_database();
            END IF;
        END LOOP;
    END IF;
END
$$;
