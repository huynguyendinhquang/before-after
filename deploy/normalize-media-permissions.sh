#!/usr/bin/env bash
set -euo pipefail

# Normalize only the managed media tree. Never use chmod -R here: operation
# markers and the reconciliation lock are private application state.
MEDIA_ROOT="${MEDIA_ROOT:-/var/lib/before-after/media}"
MEDIA_OWNER="${MEDIA_OWNER:-before-after}"
MEDIA_GROUP="${MEDIA_GROUP:-before-after-media}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "normalize-media-permissions: run as root" >&2
    exit 1
fi
if [[ ! -d "$MEDIA_ROOT" || -L "$MEDIA_ROOT" ]]; then
    echo "normalize-media-permissions: MEDIA_ROOT must be a real directory" >&2
    exit 1
fi

for directory in "$MEDIA_ROOT" originals previews derivatives quarantine; do
    if [[ "$directory" != "$MEDIA_ROOT" ]]; then
        directory="$MEDIA_ROOT/$directory"
    fi
    if [[ ! -d "$directory" || -L "$directory" ]]; then
        echo "normalize-media-permissions: missing managed directory: $directory" >&2
        exit 1
    fi
    chown "$MEDIA_OWNER:$MEDIA_GROUP" "$directory"
    chmod 2750 "$directory"
done

for directory in originals previews derivatives quarantine; do
    root="$MEDIA_ROOT/$directory"
    if find "$root" -type l -print -quit | grep -q .; then
        echo "normalize-media-permissions: symlink in managed media: $root" >&2
        exit 1
    fi
    find "$root" -mindepth 1 -type d -exec chmod 0750 {} +
    find "$root" -type f \
        ! -name '.pending-*' \
        ! -name '.capture-delete-*' \
        ! -name '.restore-*' \
        ! -name '.reconcile.lock' \
        ! -name '.upload-*.tmp' \
        -exec chmod 0640 {} +
    find "$root" -mindepth 1 -type d -exec chown "$MEDIA_OWNER:$MEDIA_GROUP" {} +
    find "$root" -type f -exec chown "$MEDIA_OWNER:$MEDIA_GROUP" {} +
    find "$root" -type f \( \
        -name '.pending-*' -o \
        -name '.capture-delete-*' -o \
        -name '.restore-*' -o \
        -name '.reconcile.lock' -o \
        -name '.upload-*.tmp' \
        \) -exec chmod 0600 {} +
done

# The lock is normally directly below MEDIA_ROOT, but keep this explicit so a
# host-side repair can never leave it group-readable.
if [[ -e "$MEDIA_ROOT/.reconcile.lock" ]]; then
    [[ ! -L "$MEDIA_ROOT/.reconcile.lock" && -f "$MEDIA_ROOT/.reconcile.lock" ]] || {
        echo "normalize-media-permissions: reconciliation lock is unsafe" >&2
        exit 1
    }
    chown "$MEDIA_OWNER:$MEDIA_GROUP" "$MEDIA_ROOT/.reconcile.lock"
    chmod 0600 "$MEDIA_ROOT/.reconcile.lock"
fi
