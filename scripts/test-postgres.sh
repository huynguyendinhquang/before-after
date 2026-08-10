#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 0 ]]; then
  echo "fixed PostgreSQL gate rejects pytest selector/filter arguments" >&2
  exit 2
fi
if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
  echo "TEST_DATABASE_URL is required for the PostgreSQL acceptance gate" >&2
  exit 2
fi

# Save gate-owned inputs before constructing the hermetic pytest environment.
test_database_url="$TEST_DATABASE_URL"
source_passfile="${PGPASSFILE:-}"
configured_build_sha="${BUILD_SHA:-}"
configured_artifact_dir="${FIXED_GATE_ARTIFACT_DIR:-}"
if [[ -n "${FIXED_GATE_ARTIFACT:-}${FIXED_GATE_OUTPUT:-}" || ( -n "${FIXED_GATE_DAC_PROOF:-}" && "$FIXED_GATE_DAC_PROOF" != "1" ) ]]; then
  echo "fixed PostgreSQL gate accepts only FIXED_GATE_ARTIFACT_DIR" >&2
  exit 2
fi

for native_client in pg_dump pg_restore psql; do
  if ! command -v "$native_client" >/dev/null 2>&1; then
    echo "$native_client is required for the PostgreSQL acceptance gate" >&2
    exit 2
  fi
  client_major=$("$native_client" --version | awk 'match($0, /(^|[[:space:]])[0-9]+([.][0-9]+|devel|beta|rc)*/) { value=substr($0, RSTART, RLENGTH); sub(/^[^0-9]*/, "", value); sub(/[.].*$/, "", value); sub(/(devel|beta|rc).*/, "", value); print value; exit }')
  if [[ "$client_major" != "${POSTGRES_CLIENT_MAJOR:-16}" ]]; then
    echo "$native_client major $client_major does not match required PostgreSQL client major ${POSTGRES_CLIENT_MAJOR:-16}" >&2
    exit 2
  fi
done

PYTHON_BIN="${PYTHON_BIN:-python3}"
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "fixed PostgreSQL gate requires a clean git working tree" >&2
  exit 2
fi

if [[ -n "$configured_build_sha" && ! "$configured_build_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "BUILD_SHA must be a lowercase 40-character commit SHA" >&2
  exit 2
fi

# Canonicalize the route before starting pytest. The raw input may contain a
# password, but no child process receives it; libpq gets a protected passfile.
path_value="${PATH:-/usr/bin:/bin}"
mapfile -t route_data < <(
  printf '%s' "$test_database_url" |
    env -i PATH="$path_value" PYTHONPATH="$repo_root" "$PYTHON_BIN" -c '
import sys
from app.db import _pgpass_line, postgres_route
route = postgres_route(sys.stdin.read())
print(route.sqlalchemy_url)
if route._password is None:
    print("0")
else:
    print("1")
    print(_pgpass_line(route))
'
)
if [[ "${#route_data[@]}" -lt 2 || -z "${route_data[0]}" ]]; then
  echo "fixed PostgreSQL gate could not canonicalize TEST_DATABASE_URL" >&2
  exit 2
fi
test_database_url="${route_data[0]}"
gate_pgpass=$(mktemp /tmp/before-after-fixed-gate-pgpass.XXXXXX)
chmod 600 -- "$gate_pgpass"
cleanup_pgpass() { rm -f -- "$gate_pgpass"; }
trap cleanup_pgpass EXIT
if [[ "${route_data[1]}" == 1 ]]; then
  printf '%s\n' "${route_data[2]}" >"$gate_pgpass"
elif [[ -n "$source_passfile" && -f "$source_passfile" && ! -L "$source_passfile" ]]; then
  cp -- "$source_passfile" "$gate_pgpass"
  chmod 600 -- "$gate_pgpass"
else
  echo "fixed PostgreSQL gate requires a password or protected PGPASSFILE" >&2
  exit 2
fi
export DATABASE_URL="$test_database_url"
export PGPASSFILE="$gate_pgpass"

old_umask=$(umask)
umask 077
if [[ -n "$configured_artifact_dir" ]]; then
  if [[ "$configured_artifact_dir" != /* ]]; then
    echo "FIXED_GATE_ARTIFACT_DIR must be an absolute external path" >&2
    exit 2
  fi
  artifact_dir=$(realpath -m -- "$configured_artifact_dir") || {
    echo "FIXED_GATE_ARTIFACT_DIR could not be resolved" >&2
    exit 2
  }
  if [[ "$artifact_dir" == "$repo_root" || "$artifact_dir" == "$repo_root/"* || "$repo_root" == "$artifact_dir/"* ]]; then
    echo "FIXED_GATE_ARTIFACT_DIR must not contain or be contained by the repository" >&2
    exit 2
  fi
  if [[ -e "$artifact_dir" || -L "$artifact_dir" ]]; then
    if [[ ! -d "$artifact_dir" || -L "$artifact_dir" ]]; then
      echo "FIXED_GATE_ARTIFACT_DIR must be a real directory" >&2
      exit 2
    fi
  else
    mkdir -p -- "$artifact_dir"
  fi
else
  artifact_dir=$(mktemp -d /tmp/before-after-fixed-gate.XXXXXX)
fi

artifact_dir=$(realpath -e -- "$artifact_dir") || {
  echo "fixed-gate artifact directory could not be resolved" >&2
  exit 2
}
if [[ "$artifact_dir" == "$repo_root" || "$artifact_dir" == "$repo_root/"* || "$repo_root" == "$artifact_dir/"* ]]; then
  echo "fixed-gate artifact directory must be external to the repository" >&2
  exit 2
fi
if [[ "$(stat -c '%u' -- "$artifact_dir")" != "$(id -u)" || "$(stat -c '%a' -- "$artifact_dir")" != "700" ]]; then
  echo "fixed-gate artifact directory must be owned by the invoking user with mode 0700" >&2
  exit 2
fi

artifact_json="$artifact_dir/slice8-fixed-gate.json"
artifact_xml="$artifact_dir/slice8-fixed-gate.junit.xml"
dac_proof="$artifact_dir/slice8-fixed-gate.dac-proof"
for artifact in "$artifact_json" "$artifact_xml" "$dac_proof" "$artifact_json.sig"; do
  if [[ -L "$artifact" ]]; then
    echo "fixed-gate artifact path must not be a symlink" >&2
    exit 2
  fi
done
evidence_root="$artifact_dir"
rm -f -- "$artifact_json" "$artifact_xml" "$dac_proof"
umask "$old_umask"
DAC_PROOF_MARKER="$dac_proof" ./scripts/test-dac.sh
export DAC_PROOF_MARKER="$dac_proof"

# Build a clean allowlist rather than trying to remember every pytest/Python
# injection variable. In particular no FIXED_GATE_*, BUILD_SHA, EVIDENCE_*,
# PYTEST_PLUGINS, PYTHONPATH, or ambient PG* variable reaches pytest.
PATH_VALUE="${PATH:-/usr/bin:/bin}"
HOME_VALUE="${HOME:-/tmp}"
env -i \
  PATH="$PATH_VALUE" \
  HOME="$HOME_VALUE" \
  LANG="${LANG:-C}" \
  LC_ALL="${LC_ALL:-C}" \
  PYTHONNOUSERSITE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  TEST_DATABASE_URL="$test_database_url" \
  DATABASE_URL="$test_database_url" \
  PGPASSFILE="$gate_pgpass" \
  DAC_PROOF_MARKER="$dac_proof" \
  "$PYTHON_BIN" -m pytest \
  tests/test_slice0.py tests/test_slice1.py tests/test_slice2.py tests/test_slice3.py \
  tests/test_slice4.py tests/test_slice5.py tests/test_slice6.py tests/test_slice7.py \
  tests/test_slice8.py --junitxml "$artifact_xml"

if [[ -n "$configured_build_sha" ]]; then
  export BUILD_SHA="$configured_build_sha"
else
  unset BUILD_SHA
fi

"$PYTHON_BIN" - "$artifact_xml" "$artifact_json" "$repo_root" "$evidence_root" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

output_path = Path(sys.argv[1]).resolve()
metadata_path = Path(sys.argv[2]).resolve()
repo_root = Path(sys.argv[3]).resolve()
evidence_root = Path(sys.argv[4]).resolve()
output_path.relative_to(evidence_root)
metadata_path.relative_to(evidence_root)
root = ET.parse(output_path).getroot()
cases = list(root.iter("testcase"))
slice8 = [case for case in cases if case.get("classname", "").endswith("test_slice8")]
if not slice8:
    raise SystemExit("fixed gate did not execute Slice 8 tests")
for suite in (root, *root.iter("testsuite")):
    if suite is root and root.tag == "testsuites" and suite.get("tests") is None:
        continue
    for field in ("failures", "errors", "skipped"):
        if int(suite.get(field, "0")) != 0:
            raise SystemExit(f"fixed gate JUnit contains {field}")
if any(case.find("failure") is not None or case.find("error") is not None for case in cases):
    raise SystemExit("fixed gate JUnit contains a failure or error")
if any(case.find("skipped") is not None for case in cases):
    raise SystemExit("fixed gate skipped a mandatory test")
required = {
    "test_actual_postgresql_backup_restore_and_authenticated_smoke",
    "test_backup_user_can_read_media_but_cannot_modify_or_delete",
}
executed = {case.get("name") for case in slice8}
if not required <= executed:
    raise SystemExit("fixed gate did not execute the named native restore and DAC tests")
configured = os.environ.get("BUILD_SHA")
if configured is not None and (len(configured) != 40 or any(char not in "0123456789abcdef" for char in configured)):
    raise SystemExit("BUILD_SHA is invalid")
try:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ).stdout.strip()
except (OSError, subprocess.CalledProcessError):
    if configured is None:
        raise SystemExit("git or BUILD_SHA is required for commit-bound evidence")
    commit = configured
if configured is not None and configured != commit:
    raise SystemExit("BUILD_SHA does not match final candidate HEAD")
try:
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repo_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ).stdout.strip()
except (OSError, subprocess.CalledProcessError):
    raise SystemExit("git is required for tree-bound evidence")
if len(tree) != 40 or any(char not in "0123456789abcdef" for char in tree):
    raise SystemExit("HEAD tree SHA is invalid")
dac_path = Path(os.environ["DAC_PROOF_MARKER"]).resolve()
dac_path.relative_to(evidence_root)
if dac_path.read_text(encoding="ascii") != "before-after.dac-proof.v1\n":
    raise SystemExit("fixed gate DAC proof marker is invalid")
metadata = {
    "format": "before-after.fixed-gate.v1",
    "completed_fixed_gate": True,
    "commit": commit,
    "tree": tree,
    "output_artifact": str(output_path.relative_to(evidence_root)),
    "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
    "dac_proof_artifact": str(dac_path.relative_to(evidence_root)),
    "dac_proof_sha256": hashlib.sha256(dac_path.read_bytes()).hexdigest(),
    "slice8_tests_executed": len(slice8),
    "mandatory_skips": 0,
}
metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="ascii")
metadata_path.chmod(0o600)
output_path.chmod(0o600)
PY
printf 'fixed PostgreSQL/DAC gate passed; artifacts=%s metadata=%s junit=%s dac=%s\n' \
  "$artifact_dir" "$artifact_json" "$artifact_xml" "$dac_proof"
