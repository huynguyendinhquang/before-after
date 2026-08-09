#!/usr/bin/env bash
set -euo pipefail

# before-after-backup.service performs stop/start through narrowly privileged
# systemd ExecStartPre/ExecStopPost commands. This process must never assume
# that a failed or unknown systemctl result means the app is stopped.
service_unit="${BACKUP_SERVICE_UNIT:-before-after.service}"
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$script_dir/.."

systemctl_bin=$(command -v systemctl || true)
if [[ -z "$systemctl_bin" ]]; then
    echo "backup failed: systemctl is required to prove the app is stopped" >&2
    exit 1
fi
set +e
service_state=$("$systemctl_bin" is-active "$service_unit" 2>/dev/null)
systemctl_status=$?
set -e
if [[ "$service_state" != "inactive" ]]; then
    echo "backup failed: systemctl did not explicitly report the app inactive" >&2
    exit 1
fi
# systemctl is-active normally returns 3 for the explicit inactive state.
# A non-inactive/unknown state is rejected above; no stop is attempted here.
if [[ "$systemctl_status" -ne 0 && "$systemctl_status" -ne 3 ]]; then
    echo "backup failed: systemctl could not establish app state" >&2
    exit 1
fi

exec python3 -m ops.backup
