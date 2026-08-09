#!/usr/bin/env bash
set -euo pipefail

# Run as root from a release checkout. This is the only helper that loads the
# production environment for Alembic/create-admin; the file is never sourced
# from a release tree or from a file writable by the app user.
RELEASE_DIR="${RELEASE_DIR:-/opt/before-after}"
APP_ENV_FILE="${APP_ENV_FILE:-/etc/before-after/before-after.env}"
MEDIA_ROOT_DEFAULT="/var/lib/before-after/media"
VENV="${VENV:-$RELEASE_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
MEDIA_HELPER="$RELEASE_DIR/deploy/media_permissions.py"

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

validate_app_environment() {
    if [[ ! -f "$APP_ENV_FILE" || -L "$APP_ENV_FILE" ]]; then
        echo "bootstrap: app environment must be a regular file" >&2
        exit 1
    fi
    if [[ "$(stat -c '%u' "$APP_ENV_FILE")" != 0 ]]; then
        echo "bootstrap: app environment must be root-owned" >&2
        exit 1
    fi
    if [[ "$(stat -c '%a' "$APP_ENV_FILE")" != 640 ]]; then
        echo "bootstrap: app environment must be mode 0640" >&2
        exit 1
    fi
    if [[ "$(stat -c '%G' "$APP_ENV_FILE")" != before-after ]]; then
        echo "bootstrap: app environment must belong to group before-after" >&2
        exit 1
    fi
}

if [[ ! -f "$MEDIA_HELPER" || -L "$MEDIA_HELPER" ]]; then
    echo "bootstrap: descriptor-based media helper is missing" >&2
    exit 1
fi
if (( ! prepare_only )); then
    validate_app_environment
    set -a
    . "$APP_ENV_FILE"
    set +a
    : "${DATABASE_URL:?DATABASE_URL is required in app environment}"
    : "${SECRET_KEY:?SECRET_KEY is required in app environment}"
fi
MEDIA_ROOT="${MEDIA_ROOT:-$MEDIA_ROOT_DEFAULT}"
if [[ "$MEDIA_ROOT" != /* ]]; then
    echo "bootstrap: MEDIA_ROOT must be absolute" >&2
    exit 1
fi
if [[ ! -x "$(command -v "$PYTHON_BIN" || true)" ]]; then
    echo "bootstrap: Python is required for descriptor-based media setup" >&2
    exit 1
fi
if (( ! prepare_only )) && [[ ! -f "$RELEASE_DIR/alembic.ini" || ! -x "$VENV/bin/alembic" || ! -x "$VENV/bin/flask" ]]; then
    echo "bootstrap: release and virtualenv must be installed first" >&2
    exit 1
fi

# Create identities only after the untrusted MEDIA_ROOT override has been
# parsed. The media helper validates its parent chain and all existing tree
# entries before creating or mutating MEDIA_ROOT.
ensure_group before-after
ensure_group before-after-backup
ensure_group before-after-media
ensure_group before-after-web
ensure_user before-after before-after "$RELEASE_DIR"
ensure_user before-after-backup before-after-backup /var/lib/before-after-backup
usermod --append --groups before-after-media,before-after-web before-after
usermod --append --groups before-after-media before-after-backup
if id -u www-data >/dev/null 2>&1; then
    # www-data must not inherit the app's secret-bearing group or media DAC.
    usermod --remove --groups before-after,before-after-media www-data
    usermod --append --groups before-after-web www-data
fi

enforce_media_group() {
    local gid member primary_gid name
    gid=$(getent group before-after-media | cut -d: -f3)
    local members
    members=$(getent group before-after-media | cut -d: -f4)
    IFS=',' read -r -a member_list <<< "$members"
    for member in "${member_list[@]}"; do
        [[ -z "$member" ]] && continue
        case "$member" in
            before-after|before-after-backup) ;;
            *) gpasswd --delete "$member" before-after-media >/dev/null 2>&1 || true ;;
        esac
    done
    while IFS=: read -r name _ _ primary_gid _; do
        if [[ "$primary_gid" == "$gid" && "$name" != before-after && "$name" != before-after-backup ]]; then
            echo "bootstrap: before-after-media is the primary group of an unapproved account: $name" >&2
            exit 1
        fi
    done < <(getent passwd)
}
enforce_media_group

install -d -o root -g root -m 0755 "$RELEASE_DIR"
"$PYTHON_BIN" "$MEDIA_HELPER" prepare \
    --root "$MEDIA_ROOT" --owner before-after --group before-after-media
install -d -o root -g before-after -m 0750 /etc/before-after

if (( prepare_only )); then
    exit 0
fi

cd "$RELEASE_DIR"
run_as_app() {
    # Start with an allowlist so root's ambient PG service/routing variables
    # cannot redirect Alembic or create-admin away from the protected file.
    local program=$1
    shift
    runuser --user before-after -- env -i \
        PATH="$VENV/bin:/usr/local/bin:/usr/bin:/bin" \
        HOME="$RELEASE_DIR" \
        /bin/bash -c '
            set -a
            source "$1"
            set +a
            : "${DATABASE_URL:?DATABASE_URL is required in app environment}"
            : "${SECRET_KEY:?SECRET_KEY is required in app environment}"
            export APP_ENV="${APP_ENV:-production}"
            export MEDIA_ROOT="${MEDIA_ROOT:-/var/lib/before-after/media}"
            export TRUSTED_PROXY_COUNT="${TRUSTED_PROXY_COUNT:-1}"
            exec "$4" "${@:5}"
        ' bash "$APP_ENV_FILE" "$VENV" "$RELEASE_DIR" "$program" "$@"
}

run_as_app "$VENV/bin/alembic" -c "$RELEASE_DIR/alembic.ini" upgrade head
run_as_app "$VENV/bin/flask" --app app:create_app create-admin
