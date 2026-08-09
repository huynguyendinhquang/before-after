#!/usr/bin/env bash
set -euo pipefail

# Run as a PostgreSQL administrator. The password is read from a protected
# file and never appears in this command's argv or process environment.
DATABASE_NAME="${DATABASE_NAME:-before_after}"
PASSWORD_FILE="${BACKUP_DB_PASSWORD_FILE:-}"
PSQL="${PSQL:-psql}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SQL_TEMPLATE="$SCRIPT_DIR/postgres-backup-role.sql"

if [[ ! "$DATABASE_NAME" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "bootstrap-postgres-backup-role: invalid database name" >&2
    exit 2
fi
if [[ -z "$PASSWORD_FILE" || ! -f "$PASSWORD_FILE" || -L "$PASSWORD_FILE" ]]; then
    echo "bootstrap-postgres-backup-role: protected password file is required" >&2
    exit 2
fi
if [[ "$(stat -c '%a' -- "$PASSWORD_FILE")" != 600 ]]; then
    echo "bootstrap-postgres-backup-role: password file must be mode 0600" >&2
    exit 2
fi
if [[ ! -f "$SQL_TEMPLATE" || -L "$SQL_TEMPLATE" ]]; then
    echo "bootstrap-postgres-backup-role: SQL template is missing" >&2
    exit 2
fi
if ! command -v "$PSQL" >/dev/null 2>&1; then
    echo "bootstrap-postgres-backup-role: psql is unavailable" >&2
    exit 2
fi

password=$(<"$PASSWORD_FILE")
if [[ -z "$password" || "$password" == *$'\n'* || "$password" == *$'\r'* ]]; then
    echo "bootstrap-postgres-backup-role: password file is invalid" >&2
    exit 2
fi
escaped_password=${password//\'/\'\'}
sql_file=$(mktemp "${TMPDIR:-/tmp}/before-after-backup-role.XXXXXX")
chmod 600 -- "$sql_file"
cleanup() {
    rm -f -- "$sql_file"
}
trap cleanup EXIT
cat "$SQL_TEMPLATE" >"$sql_file"
printf "ALTER ROLE before_after_backup PASSWORD '%s';\n" "$escaped_password" >>"$sql_file"

exec "$PSQL" --no-psqlrc --dbname "$DATABASE_NAME" \
    --set=ON_ERROR_STOP=1 --set=database_name="$DATABASE_NAME" --file "$sql_file"
