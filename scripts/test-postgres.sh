#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
  echo "TEST_DATABASE_URL is required for the PostgreSQL acceptance gate" >&2
  exit 2
fi

export DATABASE_URL="$TEST_DATABASE_URL"
python -m pytest tests/test_slice1.py tests/test_slice2.py tests/test_slice3.py "$@"
