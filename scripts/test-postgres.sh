#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
  echo "TEST_DATABASE_URL is required for the PostgreSQL acceptance gate" >&2
  exit 2
fi

for native_client in pg_dump pg_restore psql; do
  if ! command -v "$native_client" >/dev/null 2>&1; then
    echo "$native_client is required for the PostgreSQL acceptance gate" >&2
    exit 2
  fi
done

export DATABASE_URL="$TEST_DATABASE_URL"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -m pytest tests/test_slice0.py tests/test_slice1.py tests/test_slice2.py tests/test_slice3.py tests/test_slice4.py tests/test_slice5.py tests/test_slice6.py tests/test_slice7.py tests/test_slice8.py "$@"
