#!/usr/bin/env bash
set -euo pipefail

# Clinic-host evidence/proof. Run as root after bootstrap, with MEDIA_ROOT
# pointing at the live managed tree. Recursive validation is read-only and
# runs through deploy/media_permissions.py; the DAC probe is outside MEDIA_ROOT.
MEDIA_ROOT="${MEDIA_ROOT:-/var/lib/before-after/media}"
APP_USER="${APP_USER:-before-after}"
BACKUP_USER="${BACKUP_USER:-before-after-backup}"
MEDIA_GROUP="${MEDIA_GROUP:-before-after-media}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ "${EUID}" -ne 0 ]]; then
    echo "verify-permissions: run as root" >&2
    exit 1
fi
if [[ "$MEDIA_ROOT" != /* || -z "$APP_USER" || -z "$BACKUP_USER" || -z "$MEDIA_GROUP" ]]; then
    echo "verify-permissions: invalid permission environment" >&2
    exit 1
fi
for command in "$PYTHON_BIN" runuser mktemp chown chmod getent id; do
    command -v "$command" >/dev/null || {
        echo "verify-permissions: missing $command" >&2
        exit 2
    }
done

verify_media_group() {
    local record gid members member account primary_gid
    record=$(getent group "$MEDIA_GROUP") || {
        echo "verify-permissions: media group does not exist" >&2
        exit 1
    }
    IFS=: read -r _group_name _password gid members <<< "$record"
    IFS=',' read -r -a member_list <<< "$members"
    for member in "${member_list[@]}"; do
        [[ -z "$member" ]] && continue
        case "$member" in
            "$APP_USER"|"$BACKUP_USER") ;;
            *)
                echo "verify-permissions: unapproved media-group member: $member" >&2
                exit 1
                ;;
        esac
    done
    for account in "$APP_USER" "$BACKUP_USER"; do
        id -u "$account" >/dev/null 2>&1 || {
            echo "verify-permissions: approved media identity is missing: $account" >&2
            exit 1
        }
        primary_gid=$(id -g "$account")
        if [[ "$primary_gid" != "$gid" && ",$members," != *",$account,"* ]]; then
            echo "verify-permissions: approved identity lacks media-group access: $account" >&2
            exit 1
        fi
    done
    while IFS=: read -r account _ _ primary_gid _; do
        if [[ "$primary_gid" == "$gid" && "$account" != "$APP_USER" && "$account" != "$BACKUP_USER" ]]; then
            echo "verify-permissions: unapproved account has media group as primary: $account" >&2
            exit 1
        fi
    done < <(getent passwd)
}
verify_media_group

"$PYTHON_BIN" "$SCRIPT_DIR/media_permissions.py" verify \
    --root "$MEDIA_ROOT" --owner "$APP_USER" --group "$MEDIA_GROUP" \
    --require-backup-lock

probe_root=$(mktemp -d "${TMPDIR:-/tmp}/before-after-permission-proof.XXXXXX")
cleanup() {
    rm -rf -- "$probe_root" >/dev/null 2>&1 || true
}
trap cleanup EXIT
chown "$APP_USER:$MEDIA_GROUP" "$probe_root"
chmod 0750 "$probe_root"
probe_dir="$probe_root/nested"
probe="$probe_dir/clinical.bin"
runuser --user "$APP_USER" -- mkdir -- "$probe_dir"
runuser --user "$APP_USER" -- chmod 0750 -- "$probe_dir"
runuser --user "$APP_USER" -- sh -c 'printf clinical > "$1"' sh "$probe"
chown "$APP_USER:$MEDIA_GROUP" "$probe_dir" "$probe"
chmod 0640 "$probe"
runuser --user "$BACKUP_USER" -- sh -c 'test "$(cat "$1")" = clinical' sh "$probe"
if runuser --user "$BACKUP_USER" -- sh -c 'printf denied > "$1"' sh "$probe"; then
    echo "verify-permissions: backup user can modify probe media" >&2
    exit 1
fi
if runuser --user "$BACKUP_USER" -- sh -c 'touch "$1"' sh "$probe_dir/created"; then
    echo "verify-permissions: backup user can create probe media" >&2
    exit 1
fi
if runuser --user "$BACKUP_USER" -- sh -c 'rm "$1"' sh "$probe"; then
    echo "verify-permissions: backup user can delete probe media" >&2
    exit 1
fi
printf 'permissions verified: recursive modes/ownership and external DAC boundary are safe\n'
