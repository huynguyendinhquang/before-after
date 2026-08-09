#!/usr/bin/env bash
set -euo pipefail

# Clinic-host evidence/proof. Run as root after bootstrap, with MEDIA_ROOT
# pointing at the live managed tree.
MEDIA_ROOT="${MEDIA_ROOT:-/var/lib/before-after/media}"
APP_USER="${APP_USER:-before-after}"
BACKUP_USER="${BACKUP_USER:-before-after-backup}"
MEDIA_GROUP="${MEDIA_GROUP:-before-after-media}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "verify-permissions: run as root" >&2
    exit 1
fi
for command in stat runuser find; do
    command -v "$command" >/dev/null || {
        echo "verify-permissions: missing $command" >&2
        exit 2
    }
done

mode() { stat -c '%a' "$1"; }
owner() { stat -c '%U:%G' "$1"; }
assert_mode() {
    local expected=$1 path=$2 actual
    actual=$(mode "$path")
    [[ "$actual" == "$expected" ]] || {
        echo "verify-permissions: $path mode $actual, expected $expected" >&2
        exit 1
    }
}
assert_owner() {
    local expected=$1 path=$2 actual
    actual=$(owner "$path")
    [[ "$actual" == "$expected" ]] || {
        echo "verify-permissions: $path owner $actual, expected $expected" >&2
        exit 1
    }
}

[[ -d "$MEDIA_ROOT" && ! -L "$MEDIA_ROOT" ]] || exit 1
assert_mode 2750 "$MEDIA_ROOT"
assert_owner "$APP_USER:$MEDIA_GROUP" "$MEDIA_ROOT"
for name in originals previews derivatives quarantine; do
    directory="$MEDIA_ROOT/$name"
    [[ -d "$directory" && ! -L "$directory" ]] || exit 1
    assert_mode 2750 "$directory"
    assert_owner "$APP_USER:$MEDIA_GROUP" "$directory"
done

while IFS= read -r -d '' path; do
    name=${path##*/}
    case "$name" in
        .pending-*|.capture-delete-*|.reconcile.lock|.upload-*.tmp) assert_mode 600 "$path" ;;
        *) assert_mode 640 "$path" ;;
    esac
done < <(find "$MEDIA_ROOT" -type f -print0)

if [[ -e "$MEDIA_ROOT/.reconcile.lock" ]]; then
    [[ ! -L "$MEDIA_ROOT/.reconcile.lock" ]] || exit 1
    assert_mode 600 "$MEDIA_ROOT/.reconcile.lock"
fi

probe="$MEDIA_ROOT/originals/.permission-proof-$$"
runuser --user "$APP_USER" -- sh -c 'printf proof > "$1"' sh "$probe"
chmod 0640 "$probe"
runuser --user "$BACKUP_USER" -- sh -c 'test "$(cat "$1")" = proof' sh "$probe"
if runuser --user "$BACKUP_USER" -- sh -c 'printf denied > "$1"' sh "$probe"; then
    echo "verify-permissions: backup user can modify media" >&2
    exit 1
fi
if runuser --user "$BACKUP_USER" -- sh -c 'rm "$1"' sh "$probe"; then
    echo "verify-permissions: backup user can delete media" >&2
    exit 1
fi
runuser --user "$APP_USER" -- rm -- "$probe"
printf 'permissions verified: app writes; backup reads but cannot write/delete\n'
