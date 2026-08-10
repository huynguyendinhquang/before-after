#!/usr/bin/env bash
set -euo pipefail

# Disposable PostgreSQL 16 HBA integration. It edits only the named container,
# reloads the cluster, proves target pg_dump plus second-database denial, then
# restores the original pg_hba.conf before returning.
if [[ -z "${POSTGRES_HBA_DOCKER_CONTAINER:-}" ]]; then
  echo "POSTGRES_HBA_DOCKER_CONTAINER is required for the Docker HBA gate" >&2
  exit 2
fi
if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
  echo "TEST_DATABASE_URL is required for the Docker HBA gate" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker PG16 HBA gate requires a running Docker daemon" >&2
  exit 2
fi

container="$POSTGRES_HBA_DOCKER_CONTAINER"
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN="${PYTHON_BIN:-python3}"
template="$repo_root/deploy/postgres-backup-role.pg_hba.conf.template"
if [[ ! -f "$template" || -L "$template" ]]; then
  echo "HBA template is missing" >&2
  exit 2
fi

mapfile -t route_data < <(
  printf '%s' "$TEST_DATABASE_URL" |
    env -i PATH="${PATH:-/usr/bin:/bin}" PYTHONPATH="$repo_root" "$PYTHON_BIN" -c '
import sys
from app.db import postgres_route
route = postgres_route(sys.stdin.read())
settings = route.environment()
print(settings["PGDATABASE"])
print(settings["PGPORT"])
'
)
if [[ "${#route_data[@]}" -lt 2 || -z "${route_data[0]}" ]]; then
  echo "could not canonicalize TEST_DATABASE_URL" >&2
  exit 2
fi
production_database="${route_data[0]}"
if [[ ! "$production_database" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
  echo "production database name is unsafe" >&2
  exit 2
fi

second_database="before_after_hba_other_$$"
role_password=$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(24))')
role="before_after_backup"
probe_table="before_after_backup_probe"
probe_function="before_after_backup_mutator"
hba_file=$(docker exec "$container" psql --no-psqlrc --tuples-only --no-align -U postgres -d postgres -c 'SHOW hba_file' | tr -d '[:space:]')
if [[ "$hba_file" != /* || "$hba_file" == *$'\n'* ]]; then
  echo "container returned an unsafe pg_hba.conf path" >&2
  exit 2
fi

original_hba=$(mktemp /tmp/before-after-hba-original.XXXXXX)
rendered_hba=$(mktemp /tmp/before-after-hba-rendered.XXXXXX)
container_passfile="/tmp/before-after-hba-pgpass-$$"
cleanup() {
  status=$?
  set +e
  docker cp "$original_hba" "$container:$hba_file" >/dev/null 2>&1
  docker exec "$container" chown postgres:postgres "$hba_file" >/dev/null 2>&1
  docker exec "$container" chmod 640 "$hba_file" >/dev/null 2>&1
  docker exec --user postgres "$container" pg_ctl reload -D /var/lib/postgresql/data -s >/dev/null 2>&1
  docker exec "$container" rm -f "$container_passfile" /tmp/before-after-hba-proof.dump >/dev/null 2>&1
  docker exec "$container" psql --no-psqlrc --no-align -U postgres -d "$production_database" -v ON_ERROR_STOP=1 -c "DROP OWNED BY $role; REVOKE ALL PRIVILEGES ON DATABASE $production_database FROM $role" >/dev/null 2>&1
  docker exec "$container" psql --no-psqlrc --no-align -U postgres -d "$production_database" -v ON_ERROR_STOP=1 -c "DROP FUNCTION IF EXISTS public.$probe_function(); DROP TABLE IF EXISTS public.$probe_table" >/dev/null 2>&1
  docker exec "$container" psql --no-psqlrc --no-align -U postgres -d postgres -v ON_ERROR_STOP=1 -c "DROP OWNED BY $role; REVOKE ALL PRIVILEGES ON DATABASE postgres FROM $role" >/dev/null 2>&1
  docker exec "$container" psql --no-psqlrc --no-align -U postgres -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $second_database" >/dev/null 2>&1
  docker exec "$container" psql --no-psqlrc --no-align -U postgres -d postgres -v ON_ERROR_STOP=1 -c "DROP ROLE IF EXISTS $role" >/dev/null 2>&1
  rm -f -- "$original_hba" "$rendered_hba"
  exit "$status"
}
trap cleanup EXIT

docker cp "$container:$hba_file" "$original_hba" >/dev/null
{
  sed \
    -e "s/__PRODUCTION_DATABASE__/$production_database/g" \
    -e 's/__BACKUP_SERVICE_CIDR__/127.0.0.1\/32/g' \
    "$template"
  cat "$original_hba"
} >"$rendered_hba"
docker cp "$rendered_hba" "$container:$hba_file" >/dev/null
docker exec "$container" chown postgres:postgres "$hba_file"
docker exec "$container" chmod 640 "$hba_file"

# A previous interrupted disposable run may have left grants or objects in
# the target database. Clear only this test role's target-database ownership
# before replacing it; production provisioning itself remains fail-closed.
docker exec "$container" psql --no-psqlrc -U postgres -d "$production_database" -v ON_ERROR_STOP=1 -c "DO \$\$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$role') THEN EXECUTE 'DROP OWNED BY $role'; END IF; END \$\$;" >/dev/null

printf '%s\n' \
  "CREATE DATABASE $second_database;" \
  "DO \$\$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$role') THEN EXECUTE 'DROP OWNED BY $role'; END IF; END \$\$;" \
  "DROP ROLE IF EXISTS $role;" \
  "CREATE ROLE $role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD '$role_password';" \
  "ALTER ROLE $role SET default_transaction_read_only = on;" \
  "GRANT CONNECT ON DATABASE $production_database TO $role;" |
  docker exec -i "$container" psql --no-psqlrc -U postgres -d postgres -v ON_ERROR_STOP=1 -f - >/dev/null
printf '%s\n' \
  "GRANT USAGE ON SCHEMA public TO $role;" \
  "GRANT SELECT ON ALL TABLES IN SCHEMA public TO $role;" \
  "GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO $role;" |
  docker exec -i "$container" psql --no-psqlrc -U postgres -d "$production_database" -v ON_ERROR_STOP=1 -f - >/dev/null
docker exec -i "$container" psql --no-psqlrc -U postgres -d "$production_database" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
DROP FUNCTION IF EXISTS public.before_after_backup_mutator();
DROP TABLE IF EXISTS public.before_after_backup_probe;
CREATE TABLE public.before_after_backup_probe (id integer PRIMARY KEY);
CREATE OR REPLACE FUNCTION public.before_after_backup_mutator()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  INSERT INTO public.before_after_backup_probe VALUES (1);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.before_after_backup_mutator() FROM PUBLIC;
GRANT SELECT ON public.before_after_backup_probe TO before_after_backup;
SQL
printf '*:*:*:%s:%s\n' "$role" "$role_password" |
  docker exec -i "$container" sh -ceu "umask 077; cat > '$container_passfile'"

docker exec "$container" psql --no-psqlrc --tuples-only --no-align -U postgres -d postgres -c 'SELECT pg_reload_conf()' >/dev/null
sleep 1
server_major=$(docker exec "$container" psql --no-psqlrc --tuples-only --no-align -U postgres -d postgres -c 'SHOW server_version_num' | tr -d '[:space:]')
[[ "$server_major" == 16* ]] || { echo "Docker HBA gate requires PostgreSQL 16" >&2; exit 1; }

backup_exec=(docker exec "$container" env "PGHOST=/var/run/postgresql" "PGPORT=5432" "PGUSER=$role" "PGPASSFILE=$container_passfile" psql --no-psqlrc --no-password)
"${backup_exec[@]}" --dbname "$production_database" --command 'SELECT 1' >/dev/null
"${backup_exec[@]}" --dbname "$production_database" --command "SELECT count(*) FROM public.$probe_table" >/dev/null
for mutation in \
  "INSERT INTO public.$probe_table VALUES (1)" \
  "UPDATE public.$probe_table SET id = 2" \
  "DELETE FROM public.$probe_table" \
  "CREATE TABLE public.before_after_backup_ddl_probe (id integer)" \
  "SELECT public.$probe_function()"; do
  if "${backup_exec[@]}" --dbname "$production_database" --command "$mutation" >/dev/null 2>&1; then
    echo "backup role mutation unexpectedly succeeded: $mutation" >&2
    exit 1
  fi
done
pg_dump_exec=(docker exec "$container" env "PGHOST=/var/run/postgresql" "PGPORT=5432" "PGUSER=$role" "PGPASSFILE=$container_passfile" pg_dump --no-password --format=custom --file=/tmp/before-after-hba-proof.dump --dbname="$production_database")
"${pg_dump_exec[@]}" >/dev/null
if "${backup_exec[@]}" --dbname "$second_database" --command 'SELECT 1' >/dev/null 2>&1; then
  echo "backup role connected to the second database" >&2
  exit 1
fi

# Read the parsed rules after reload; a nonzero count is required for every
# expected class, not merely a successful target connection.
rule_counts=$(docker exec "$container" psql --no-psqlrc --tuples-only --no-align -U postgres -d postgres -c "
SELECT
  (SELECT count(*) FROM pg_hba_file_rules WHERE type = 'local' AND database = ARRAY['$production_database'] AND user_name = ARRAY['$role'] AND auth_method = 'scram-sha-256') || ':' ||
  (SELECT count(*) FROM pg_hba_file_rules WHERE type = 'local' AND database = ARRAY['all'] AND user_name = ARRAY['$role'] AND auth_method = 'reject') || ':' ||
  (SELECT count(*) FROM pg_hba_file_rules WHERE type = 'host' AND database = ARRAY['$production_database'] AND user_name = ARRAY['$role'] AND address = '127.0.0.1' AND netmask = '255.255.255.255') || ':' ||
  (SELECT count(*) FROM pg_hba_file_rules WHERE type = 'host' AND database = ARRAY['all'] AND user_name = ARRAY['$role'] AND address = '0.0.0.0' AND auth_method = 'reject') || ':' ||
  (SELECT count(*) FROM pg_hba_file_rules WHERE type = 'host' AND database = ARRAY['all'] AND user_name = ARRAY['$role'] AND address = '::' AND auth_method = 'reject')")
[[ "$rule_counts" == "1:1:1:1:1" ]] || { echo "parsed HBA rule proof failed" >&2; exit 1; }
printf 'Docker PostgreSQL 16 HBA isolation passed for %s; second database denied\n' "$production_database"
