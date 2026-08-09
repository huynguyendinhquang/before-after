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

./scripts/test-dac.sh

export DATABASE_URL="$TEST_DATABASE_URL"
PYTHON_BIN="${PYTHON_BIN:-python3}"
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
artifact_json="${FIXED_GATE_ARTIFACT:-$repo_root/artifacts/slice8-fixed-gate.json}"
artifact_xml="${FIXED_GATE_OUTPUT:-${artifact_json%.json}.junit.xml}"
dac_proof="${FIXED_GATE_DAC_PROOF:-${artifact_json%.json}.dac-proof}"
mkdir -p "$(dirname -- "$artifact_json")" "$(dirname -- "$artifact_xml")" "$(dirname -- "$dac_proof")"
chmod 700 "$(dirname -- "$artifact_json")" "$(dirname -- "$artifact_xml")" "$(dirname -- "$dac_proof")"
evidence_root=$(CDPATH= cd -- "$(dirname -- "$artifact_json")" && pwd)
rm -f -- "$artifact_json" "$artifact_xml" "$dac_proof"
DAC_PROOF_MARKER="$dac_proof" ./scripts/test-dac.sh
export DAC_PROOF_MARKER="$dac_proof"

# PYTEST_ADDOPTS is cleared so a caller cannot inject -k/--deselect into the
# fixed suite. The DAC test case below checks the proof emitted by the
# mandatory disposable container test.
env PYTEST_ADDOPTS= FIXED_GATE_DAC_PROOF=1 DAC_PROOF_MARKER="$dac_proof" "$PYTHON_BIN" -m pytest \
  tests/test_slice0.py tests/test_slice1.py tests/test_slice2.py tests/test_slice3.py \
  tests/test_slice4.py tests/test_slice5.py tests/test_slice6.py tests/test_slice7.py \
  tests/test_slice8.py --junitxml "$artifact_xml"

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
dac_path = Path(os.environ["DAC_PROOF_MARKER"]).resolve()
dac_path.relative_to(evidence_root)
if dac_path.read_text(encoding="ascii") != "before-after.dac-proof.v1\n":
    raise SystemExit("fixed gate DAC proof marker is invalid")
metadata = {
    "format": "before-after.fixed-gate.v1",
    "completed_fixed_gate": True,
    "commit": commit,
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
printf 'fixed PostgreSQL/DAC gate passed; evidence=%s\n' "$artifact_json"
