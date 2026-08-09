#!/usr/bin/env bash
set -euo pipefail

# Mandatory disposable effective-DAC proof for the all-slice gate. A missing
# Docker daemon is a gate failure, not a pytest skip.
if ! command -v docker >/dev/null 2>&1; then
    echo "DAC proof failed: docker is required" >&2
    exit 2
fi
if ! docker info >/dev/null 2>&1; then
    echo "DAC proof failed: Docker daemon is unavailable" >&2
    exit 2
fi

image="${DAC_TEST_IMAGE:-alpine:3.20}"
docker run --rm --privileged --network none "$image" sh -ceu "$(cat <<'CONTAINER'
addgroup -S before-after
addgroup -S before-after-backup
addgroup -S before-after-media
adduser -S -D -H -s /sbin/nologin -G before-after before-after
adduser -S -D -H -s /sbin/nologin -G before-after-backup before-after-backup
addgroup before-after before-after-media
addgroup before-after-backup before-after-media

media=/var/lib/before-after/media
mkdir -p "$media"/originals "$media"/previews "$media"/derivatives "$media"/quarantine
chown -R before-after:before-after-media "$media"
find "$media" -type d -exec chmod 2750 {} +

su before-after -s /bin/sh -c 'printf clinical > /var/lib/before-after/media/originals/proof.bin'
chmod 0640 "$media/originals/proof.bin"
su before-after -s /bin/sh -c 'mkdir /var/lib/before-after/media/originals/nested && chmod 0750 /var/lib/before-after/media/originals/nested && printf nested > /var/lib/before-after/media/originals/nested/proof.bin'
chown -R before-after:before-after-media "$media/originals/nested"
chmod 0640 "$media/originals/nested/proof.bin"
su before-after-backup -s /bin/sh -c 'test "$(cat /var/lib/before-after/media/originals/proof.bin)" = clinical'
su before-after-backup -s /bin/sh -c 'test "$(cat /var/lib/before-after/media/originals/nested/proof.bin)" = nested'
if su before-after-backup -s /bin/sh -c 'printf denied > /var/lib/before-after/media/originals/proof.bin' 2>/dev/null; then
    echo "backup user can modify clinical media" >&2
    exit 1
fi
if su before-after-backup -s /bin/sh -c 'rm /var/lib/before-after/media/originals/proof.bin' 2>/dev/null; then
    echo "backup user can delete clinical media" >&2
    exit 1
fi
if su before-after-backup -s /bin/sh -c 'printf denied > /var/lib/before-after/media/originals/nested/created.bin' 2>/dev/null; then
    echo "backup user can create nested clinical media" >&2
    exit 1
fi
if su before-after-backup -s /bin/sh -c 'rm /var/lib/before-after/media/originals/nested/proof.bin' 2>/dev/null; then
    echo "backup user can delete nested clinical media" >&2
    exit 1
fi
su before-after -s /bin/sh -c 'rm /var/lib/before-after/media/originals/proof.bin'
su before-after -s /bin/sh -c 'rm -rf /var/lib/before-after/media/originals/nested'
CONTAINER
)"

if [[ -n "${DAC_PROOF_MARKER:-}" ]]; then
  marker_dir=$(dirname -- "$DAC_PROOF_MARKER")
  mkdir -p -- "$marker_dir"
  temporary="$DAC_PROOF_MARKER.$$.tmp"
  printf 'before-after.dac-proof.v1\n' >"$temporary"
  chmod 0600 "$temporary"
  mv -f -- "$temporary" "$DAC_PROOF_MARKER"
fi
