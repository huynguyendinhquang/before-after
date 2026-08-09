#!/usr/bin/env bash
set -euo pipefail

# Normalize only the managed media tree. The helper does every recursive
# operation through no-follow directory descriptors.
MEDIA_ROOT="${MEDIA_ROOT:-/var/lib/before-after/media}"
MEDIA_OWNER="${MEDIA_OWNER:-before-after}"
MEDIA_GROUP="${MEDIA_GROUP:-before-after-media}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ "${EUID}" -ne 0 ]]; then
    echo "normalize-media-permissions: run as root" >&2
    exit 1
fi
if [[ "$MEDIA_ROOT" != /* || -z "$MEDIA_OWNER" || -z "$MEDIA_GROUP" ]]; then
    echo "normalize-media-permissions: invalid MEDIA_ROOT or ownership environment" >&2
    exit 1
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "normalize-media-permissions: Python interpreter is unavailable" >&2
    exit 2
fi
if [[ ! -d "$MEDIA_ROOT" || -L "$MEDIA_ROOT" ]]; then
    echo "normalize-media-permissions: MEDIA_ROOT must be a real directory" >&2
    exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/media_permissions.py" normalize \
    --root "$MEDIA_ROOT" --owner "$MEDIA_OWNER" --group "$MEDIA_GROUP"
