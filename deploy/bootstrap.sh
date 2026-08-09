#!/usr/bin/env bash
set -euo pipefail

# Run as root from a release checkout. This is the only helper that loads the
# production environment for Alembic/create-admin; the file is never sourced
# from a release tree or from a file writable by the app user.
RELEASE_DIR="${RELEASE_DIR:-/opt/before-after}"
APP_ENV_FILE="${APP_ENV_FILE:-/etc/before-after/before-after.env}"
MEDIA_ROOT_DEFAULT="/var/lib/before-after/media"
VENV="${VENV:-$RELEASE_DIR/.venv}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "bootstrap: run as root" >&2
    exit 1
fi

usage() {
    printf 'usage: %s [--prepare-only]\n' "$0" >&2
}
prepare_only=0
if [[ "${1:-}" == "--prepare-only" ]]; then
    prepare_only=1
    shift
fi
if [[ "$#" -ne 0 ]]; then
    usage
    exit 2
fi

ensure_group() {
    local name=$1
    if ! getent group "$name" >/dev/null; then
        groupadd --system "$name"
    fi
}

ensure_user() {
    local name=$1 group=$2 home=$3
    if ! id -u "$name" >/dev/null 2>&1; then
        useradd --system --home-dir "$home" --gid "$group" --shell /usr/sbin/nologin "$name"
    else
        usermod --gid "$group" --shell /usr/sbin/nologin "$name"
    fi
}

# Create identities before any install/chown operation below.
ensure_group before-after
ensure_group before-after-backup
ensure_group before-after-media
ensure_user before-after before-after "$RELEASE_DIR"
ensure_user before-after-backup before-after-backup /var/lib/before-after-backup
usermod --append --groups before-after-media before-after
usermod --append --groups before-after-media before-after-backup
if id -u www-data >/dev/null 2>&1; then
    usermod --append --groups before-after www-data
fi

install -d -o root -g root -m 0755 "$RELEASE_DIR"
install -d -o before-after -g before-after-media -m 2750 "$MEDIA_ROOT_DEFAULT"
for media_dir in originals previews derivatives quarantine; do
    install -d -o before-after -g before-after-media -m 2750 "$MEDIA_ROOT_DEFAULT/$media_dir"
done
MEDIA_ROOT="$MEDIA_ROOT_DEFAULT" "$RELEASE_DIR/deploy/normalize-media-permissions.sh"
install -d -o root -g root -m 0750 /etc/before-after

if (( prepare_only )); then
    exit 0
fi

if [[ ! -f "$RELEASE_DIR/alembic.ini" || ! -x "$VENV/bin/alembic" || ! -x "$VENV/bin/flask" ]]; then
    echo "bootstrap: release and virtualenv must be installed first" >&2
    exit 1
fi

if [[ ! -f "$APP_ENV_FILE" || -L "$APP_ENV_FILE" ]]; then
    echo "bootstrap: app environment must be a regular file" >&2
    exit 1
fi
if [[ "$(stat -c '%u' "$APP_ENV_FILE")" != 0 ]]; then
    echo "bootstrap: app environment must be root-owned" >&2
    exit 1
fi
# 0640 is intentional: root owns the file and only before-after reads it.
env_mode=$(stat -c '%a' "$APP_ENV_FILE")
if [[ "$env_mode" != 640 ]]; then
    echo "bootstrap: app environment must be mode 0640 (got $env_mode)" >&2
    exit 1
fi
if [[ "$(stat -c '%G' "$APP_ENV_FILE")" != before-after ]]; then
    echo "bootstrap: app environment must belong to group before-after" >&2
    exit 1
fi

# The file is root-owned and mode-checked before sourcing. Export only the
# settings needed by the unprivileged commands, rather than preserving root's
# whole environment.
set -a
. "$APP_ENV_FILE"
set +a
: "${DATABASE_URL:?DATABASE_URL is required in app environment}"
: "${SECRET_KEY:?SECRET_KEY is required in app environment}"
MEDIA_ROOT="${MEDIA_ROOT:-$MEDIA_ROOT_DEFAULT}"
APP_ENV="${APP_ENV:-production}"
TRUSTED_PROXY_COUNT="${TRUSTED_PROXY_COUNT:-1}"

install -d -o before-after -g before-after-media -m 2750 "$MEDIA_ROOT"
for media_dir in originals previews derivatives quarantine; do
    install -d -o before-after -g before-after-media -m 2750 "$MEDIA_ROOT/$media_dir"
done
MEDIA_ROOT="$MEDIA_ROOT" "$RELEASE_DIR/deploy/normalize-media-permissions.sh"

cd "$RELEASE_DIR"
run_as_app() {
    runuser --user before-after -- env \
        APP_ENV="$APP_ENV" \
        DATABASE_URL="$DATABASE_URL" \
        MEDIA_ROOT="$MEDIA_ROOT" \
        SECRET_KEY="$SECRET_KEY" \
        TRUSTED_PROXY_COUNT="$TRUSTED_PROXY_COUNT" \
        "$@"
}

run_as_app "$VENV/bin/alembic" -c "$RELEASE_DIR/alembic.ini" upgrade head
run_as_app "$VENV/bin/flask" --app app:create_app create-admin
