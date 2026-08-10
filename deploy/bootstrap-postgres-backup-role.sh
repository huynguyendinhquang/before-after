#!/usr/bin/env bash
set -euo pipefail

# Run as a PostgreSQL administrator. The password is read from a protected
# file and never appears in this command's argv or process environment.
DATABASE_NAME="${DATABASE_NAME:-before_after}"
APP_OWNER="${APP_OWNER:-before_after}"
PASSWORD_FILE="${BACKUP_DB_PASSWORD_FILE:-}"
PSQL="${PSQL:-psql}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SQL_TEMPLATE="$SCRIPT_DIR/postgres-backup-role.sql"

valid_identifier() {
    [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]
}

if ! valid_identifier "$DATABASE_NAME" || ! valid_identifier "$APP_OWNER"; then
    echo "bootstrap-postgres-backup-role: invalid database or app-owner name" >&2
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

# Ask the administrator connection for every non-template database that allows
# connections. Each database gets its own revoke/check transaction; PUBLIC
# CONNECT therefore never grants the backup role access to another database's
# tables or schema.
mapfile -t database_names < <(
    "$PSQL" --no-psqlrc --tuples-only --no-align --dbname "$DATABASE_NAME" \
        --set=ON_ERROR_STOP=1 \
        --command "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname"
)
if [[ "${#database_names[@]}" -eq 0 ]]; then
    echo "bootstrap-postgres-backup-role: administrator cannot enumerate databases" >&2
    exit 1
fi

for database in "${database_names[@]}"; do
    [[ -n "$database" ]] || continue
    if ! valid_identifier "$database"; then
        echo "bootstrap-postgres-backup-role: unsafe database name from catalog" >&2
        exit 1
    fi
    if [[ "$database" == "$DATABASE_NAME" ]]; then
        target=on
    else
        target=off
    fi
    "$PSQL" --no-psqlrc --dbname "$database" \
        --set=ON_ERROR_STOP=1 \
        --set=database_name="$database" \
        --set=production_database="$DATABASE_NAME" \
        --set=app_owner="$APP_OWNER" \
        --set=is_target="$target" \
        --file "$SQL_TEMPLATE"
done

# The SQL helper creates the role before this statement, so first-run and
# reruns are both valid. Keep the password in a protected temporary file.
sql_file=$(mktemp "${TMPDIR:-/tmp}/before-after-backup-role.XXXXXX")
chmod 600 -- "$sql_file"
cleanup() {
    rm -f -- "$sql_file"
}
trap cleanup EXIT
printf "ALTER ROLE before_after_backup PASSWORD '%s';\n" "$escaped_password" >"$sql_file"
"$PSQL" --no-psqlrc --dbname "$DATABASE_NAME" \
    --set=ON_ERROR_STOP=1 --file "$sql_file"
