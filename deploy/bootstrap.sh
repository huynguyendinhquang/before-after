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

fail_bootstrap() {
    echo "bootstrap: $*" >&2
    exit 1
}

ensure_group() {
    local name=$1 record gid group_name
    if ! record=$(getent group "$name"); then
        groupadd --system "$name"
        record=$(getent group "$name") || fail_bootstrap "group creation did not persist: $name"
    fi
    IFS=: read -r group_name _ gid _ <<< "$record"
    [[ "$group_name" == "$name" && "$gid" != 0 ]] || fail_bootstrap "unsafe group identity: $name"
}

validate_user_identity() {
    local name=$1 expected_group=$2 expected_home=$3 record account uid gid home shell
    record=$(getent passwd "$name") || fail_bootstrap "user is missing: $name"
    IFS=: read -r account _ uid gid _ home shell <<< "$record"
    [[ "$account" == "$name" ]] || fail_bootstrap "user identity is an alias: $name"
    [[ "$uid" != 0 ]] || fail_bootstrap "user must not be root: $name"
    [[ "$gid" == "$(getent group "$expected_group" | cut -d: -f3)" ]] || fail_bootstrap "user has the wrong primary group: $name"
    [[ "$home" == "$expected_home" ]] || fail_bootstrap "user has the wrong home: $name"
    [[ "$shell" == /usr/sbin/nologin ]] || fail_bootstrap "user has an interactive shell: $name"
    while IFS=: read -r account _ other_uid _ _ _ _; do
        if [[ "$other_uid" == "$uid" && "$account" != "$name" ]]; then
            fail_bootstrap "user UID is aliased by $account: $name"
        fi
    done < <(getent passwd)
}

ensure_user() {
    local name=$1 group=$2 home=$3
    if ! getent passwd "$name" >/dev/null; then
        useradd --system --home-dir "$home" --gid "$group" --shell /usr/sbin/nologin "$name"
    else
        usermod --gid "$group" --home "$home" --shell /usr/sbin/nologin "$name"
    fi
    validate_user_identity "$name" "$group" "$home"
}

set_exact_groups() {
    local name=$1 groups=$2
    # --groups replaces supplementary memberships; it is intentionally not --append.
    usermod --groups "$groups" "$name"
}

validate_exact_groups() {
    local name=$1 expected=$2 actual
    actual=$(id -G "$name" | tr ' ' '\n' | sort -n | paste -sd, -)
    expected=$(printf '%s\n' "$expected" | tr ',' '\n' | sort -n | paste -sd, -)
    [[ "$actual" == "$expected" ]] || fail_bootstrap "unexpected supplementary groups for $name"
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
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
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
media_gid=$(getent group before-after-media | cut -d: -f3)
web_gid=$(getent group before-after-web | cut -d: -f3)
app_gid=$(getent group before-after | cut -d: -f3)
backup_gid=$(getent group before-after-backup | cut -d: -f3)
if [[ "$app_gid" == "$media_gid" || "$app_gid" == "$web_gid" || "$backup_gid" == "$media_gid" || "$backup_gid" == "$web_gid" || "$media_gid" == "$web_gid" ]]; then
    fail_bootstrap "managed groups are aliased"
fi
set_exact_groups before-after "$media_gid,$web_gid"
set_exact_groups before-after-backup "$media_gid"
validate_exact_groups before-after "$app_gid,$media_gid,$web_gid"
validate_exact_groups before-after-backup "$backup_gid,$media_gid"
if id -nG before-after-backup | grep -Eq '(^| )(before-after|before-after-web|before-after-secrets|secrets)( |$)'; then
    fail_bootstrap "backup user remains in a secret or web group"
fi
if id -u www-data >/dev/null 2>&1; then
    # www-data keeps unrelated distro groups, but never app secret/media groups.
    www_primary_gid=$(id -g www-data)
    if [[ "$www_primary_gid" == "$app_gid" || "$www_primary_gid" == "$media_gid" ]]; then
        fail_bootstrap "www-data has a protected application primary group"
    fi
    usermod --remove --groups before-after,before-after-media www-data
    for secret_group in before-after-secrets before-after-secret secrets; do
        if getent group "$secret_group" >/dev/null && id -nG www-data | grep -Eq "(^| )${secret_group}( |$)"; then
            gpasswd --delete www-data "$secret_group"
        fi
    done
    usermod --append --groups before-after-web www-data
    if id -nG www-data | grep -Eq '(^| )(before-after|before-after-media|before-after-secrets|before-after-secret|secrets)( |$)'; then
        fail_bootstrap "www-data remains in a protected application group"
    fi
    if ! id -nG www-data | grep -Eq '(^| )before-after-web( |$)'; then
        fail_bootstrap "www-data was not added to before-after-web"
    fi
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
            *) gpasswd --delete "$member" before-after-media >/dev/null 2>&1 ;;
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
    # Never source "$1" in the child: canonicalize and protect credentials first.
    local program=$1
    shift
    local -a route_data
    mapfile -t route_data < <(
        printf '%s' "$DATABASE_URL" |
            env -i PATH="$VENV/bin:/usr/local/bin:/usr/bin:/bin" PYTHONPATH="$RELEASE_DIR" \
            "$VENV/bin/python" -c '
import sys
from app.db import _pgpass_line, postgres_route
route = postgres_route(sys.stdin.read())
print(route.credential_free_url)
if route._password is None:
    print("0")
else:
    print("1")
    print(_pgpass_line(route))
'
    )
    if [[ "${#route_data[@]}" -lt 2 || -z "${route_data[0]}" ]]; then
        echo "bootstrap: could not canonicalize DATABASE_URL" >&2
        exit 1
    fi
    local canonical_url=${route_data[0]}
    local passfile=${PGPASSFILE:-}
    local temporary_passfile=
    if [[ "${route_data[1]}" == 1 ]]; then
        temporary_passfile=$(mktemp "${TMPDIR:-/tmp}/before-after-app-pgpass.XXXXXX")
        chmod 0600 -- "$temporary_passfile"
        printf '%s\n' "${route_data[2]}" >"$temporary_passfile"
        chown before-after:before-after "$temporary_passfile"
        passfile=$temporary_passfile
    elif [[ -z "$passfile" || ! -f "$passfile" || -L "$passfile" ]]; then
        echo "bootstrap: PGPASSFILE is required when DATABASE_URL has no password" >&2
        exit 1
    fi
    local status
    if runuser --user before-after -- env -i \
        PATH="$VENV/bin:/usr/local/bin:/usr/bin:/bin" \
        HOME="$RELEASE_DIR" \
        APP_ENV="$APP_ENV" \
        DATABASE_URL="$canonical_url" \
        MEDIA_ROOT="$MEDIA_ROOT" \
        SECRET_KEY="${SECRET_KEY}" \
        TRUSTED_PROXY_COUNT="$TRUSTED_PROXY_COUNT" \
        PGPASSFILE="$passfile" \
        /bin/bash -c '
            exec "$1" "${@:2}"
        ' bash "$program" "$@"; then
        status=0
    else
        status=$?
    fi
    if [[ -n "$temporary_passfile" ]]; then
        rm -f -- "$temporary_passfile"
    fi
    return "$status"
}

run_as_app "$VENV/bin/alembic" -c "$RELEASE_DIR/alembic.ini" upgrade head
run_as_app "$VENV/bin/flask" --app app:create_app create-admin
