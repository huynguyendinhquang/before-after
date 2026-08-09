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

mode() { stat -c '%a' -- "$1"; }
owner() { stat -c '%U:%G' -- "$1"; }
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

[[ -d "$MEDIA_ROOT" && ! -L "$MEDIA_ROOT" ]] || {
    echo "verify-permissions: MEDIA_ROOT must be a real directory" >&2
    exit 1
}
assert_mode 2750 "$MEDIA_ROOT"
assert_owner "$APP_USER:$MEDIA_GROUP" "$MEDIA_ROOT"

for name in originals previews derivatives quarantine; do
    directory="$MEDIA_ROOT/$name"
    [[ -d "$directory" && ! -L "$directory" ]] || {
        echo "verify-permissions: missing managed directory: $directory" >&2
        exit 1
    }
    assert_mode 2750 "$directory"
    assert_owner "$APP_USER:$MEDIA_GROUP" "$directory"
done

# Reject links and every non-directory/non-regular entry before checking modes.
unsafe=$(find -P "$MEDIA_ROOT" \( -type l -o \( ! -type d ! -type f \) \) -print -quit)
if [[ -n "$unsafe" ]]; then
    echo "verify-permissions: symlink or special file: $unsafe" >&2
    exit 1
fi

while IFS= read -r -d '' directory; do
    if [[ "$directory" == "$MEDIA_ROOT" || "$directory" == "$MEDIA_ROOT/originals" ||
          "$directory" == "$MEDIA_ROOT/previews" || "$directory" == "$MEDIA_ROOT/derivatives" ||
          "$directory" == "$MEDIA_ROOT/quarantine" ]]; then
        expected=2750
    else
        expected=750
    fi
    assert_mode "$expected" "$directory"
    assert_owner "$APP_USER:$MEDIA_GROUP" "$directory"
done < <(find -P "$MEDIA_ROOT" -type d -print0)

while IFS= read -r -d '' path; do
    name=${path##*/}
    case "$name" in
        .pending-*|.capture-delete-*|.restore-*|.reconcile.lock|.upload-*.tmp)
            assert_mode 600 "$path"
            ;;
        .*)
            echo "verify-permissions: unknown operational file: $path" >&2
            exit 1
            ;;
        *)
            assert_mode 640 "$path"
            ;;
    esac
    assert_owner "$APP_USER:$MEDIA_GROUP" "$path"
done < <(find -P "$MEDIA_ROOT" -type f -print0)

# Prove the actual DAC boundary at a nested path: app writes, backup reads,
# and backup can neither create nor delete there.
probe_dir="$MEDIA_ROOT/originals/.permission-proof-nested-$$"
probe="$probe_dir/clinical.bin"
cleanup() {
    runuser --user "$APP_USER" -- rm -rf -- "$probe_dir" >/dev/null 2>&1 || true
}
trap cleanup EXIT
runuser --user "$APP_USER" -- mkdir -- "$probe_dir"
runuser --user "$APP_USER" -- chmod 0750 -- "$probe_dir"
runuser --user "$APP_USER" -- sh -c 'printf clinical > "$1"' sh "$probe"
chown "$APP_USER:$MEDIA_GROUP" "$probe_dir" "$probe"
chmod 0640 "$probe"
runuser --user "$BACKUP_USER" -- sh -c 'test "$(cat "$1")" = clinical' sh "$probe"
if runuser --user "$BACKUP_USER" -- sh -c 'printf denied > "$1"' sh "$probe"; then
    echo "verify-permissions: backup user can modify nested media" >&2
    exit 1
fi
if runuser --user "$BACKUP_USER" -- sh -c 'touch "$1"' sh "$probe_dir/created"; then
    echo "verify-permissions: backup user can create nested media" >&2
    exit 1
fi
if runuser --user "$BACKUP_USER" -- sh -c 'rm "$1"' sh "$probe"; then
    echo "verify-permissions: backup user can delete nested media" >&2
    exit 1
fi
printf 'permissions verified: recursive modes/ownership and nested DAC boundary are safe\n'
