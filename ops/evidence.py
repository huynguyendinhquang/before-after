#!/usr/bin/env python3
"""Generate non-sensitive local evidence for a Slice 8 issue."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


REQUIRED_FIXED_GATE_TESTS = frozenset(
    {
        "test_actual_postgresql_backup_restore_and_authenticated_smoke",
        "test_backup_user_can_read_media_but_cannot_modify_or_delete",
    }
)


CHECK_COMMANDS = {
    "shell_syntax": [
        "bash",
        "-c",
        "for script in \"$@\"; do bash -n \"$script\" || exit; done",
        "bash",
        "deploy/bootstrap.sh",
        "ops/backup.sh",
        "deploy/normalize-media-permissions.sh",
        "deploy/verify-permissions.sh",
        "scripts/test-dac.sh",
        "scripts/test-postgres.sh",
    ],
    "python_compile": [sys.executable, "-m", "compileall", "-q", "app", "migrations", "ops", "tests"],
}


def _run(command: list[str], repo_root: Path) -> dict[str, object]:
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return {"command": command, "passed": False, "error": type(exc).__name__}
    return {
        "command": command,
        "passed": result.returncode == 0,
        "returncode": result.returncode,
    }


def _repository_commit(repo_root: Path) -> str | None:
    configured = os.environ.get("BUILD_SHA")
    if configured is not None:
        return configured if re.fullmatch(r"[0-9a-f]{40}", configured) else None
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _junit_summary(path: Path) -> tuple[set[str], int, int, int, int]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError("fixed gate JUnit output is invalid") from exc
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("fixed gate JUnit output contains no test cases")
    failures = errors = skipped = 0
    for element in root.iter():
        if element.tag not in {"testsuite", "testsuites"}:
            continue
        for field, label in (("failures", "failures"), ("errors", "errors"), ("skipped", "skips")):
            raw = element.get(field, "0")
            try:
                count = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"fixed gate JUnit {label} count is invalid") from exc
            if count < 0:
                raise ValueError(f"fixed gate JUnit {label} count is invalid")
            if field == "failures":
                failures = max(failures, count)
            elif field == "errors":
                errors = max(errors, count)
            else:
                skipped = max(skipped, count)
    slice8_names: set[str] = set()
    slice8_count = 0
    for case in cases:
        name = case.get("name")
        if case.get("classname", "").endswith("test_slice8"):
            slice8_count += 1
            if isinstance(name, str):
                slice8_names.add(name)
        if case.find("failure") is not None:
            failures += 1
        if case.find("error") is not None:
            errors += 1
        if case.find("skipped") is not None:
            skipped += 1
    return slice8_names, slice8_count, failures, errors, skipped


def _fixed_gate_evidence(repo_root: Path) -> tuple[dict[str, object], bool]:
    """Accept runtime success only with a commit-bound, parsed fixed gate."""
    configured = os.environ.get("FIXED_GATE_ARTIFACT")
    artifact = Path(configured) if configured else repo_root / "artifacts/slice8-fixed-gate.json"
    if not artifact.is_absolute():
        artifact = repo_root / artifact
    artifact = artifact.absolute()
    try:
        readable = artifact.is_file() and not artifact.is_symlink()
        artifact.relative_to(repo_root.absolute())
    except (OSError, ValueError):
        readable = False
    if not readable:
        return (
            {
                "status": "not_run",
                "reason": "fixed PostgreSQL/DAC gate artifact is absent; syntax-only checks do not prove runtime",
            },
            False,
        )
    try:
        value = json.loads(artifact.read_text(encoding="ascii"))
        output_name = value["output_artifact"]
        output_sha256 = value["output_sha256"]
        if not isinstance(output_name, str):
            raise ValueError
        output_relative = Path(output_name)
        if output_relative.is_absolute() or ".." in output_relative.parts:
            raise ValueError
        output_candidate = repo_root / output_relative
        if output_candidate.is_symlink():
            raise ValueError
        output = output_candidate.resolve(strict=False)
        output.relative_to(repo_root.resolve())
        expected_commit = _repository_commit(repo_root)
        if expected_commit is None or value.get("commit") != expected_commit:
            raise ValueError("fixed gate commit does not match this build")
        slice8_names, slice8_count, failures, errors, xml_skips = _junit_summary(output)
        valid = (
            value.get("format") == "before-after.fixed-gate.v1"
            and value.get("completed_fixed_gate") is True
            and isinstance(value.get("mandatory_skips"), int)
            and not isinstance(value.get("mandatory_skips"), bool)
            and value["mandatory_skips"] == 0
            and isinstance(value.get("slice8_tests_executed"), int)
            and not isinstance(value.get("slice8_tests_executed"), bool)
            and value["slice8_tests_executed"] == slice8_count
            and REQUIRED_FIXED_GATE_TESTS <= slice8_names
            and failures == 0
            and errors == 0
            and xml_skips == 0
            and not output.is_symlink()
            and output.is_file()
            and isinstance(output_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", output_sha256) is not None
            and hashlib.sha256(output.read_bytes()).hexdigest() == output_sha256
        )
        dac_name = value["dac_proof_artifact"]
        dac_sha256 = value["dac_proof_sha256"]
        if not isinstance(dac_name, str) or not isinstance(dac_sha256, str):
            raise ValueError
        dac_relative = Path(dac_name)
        if dac_relative.is_absolute() or ".." in dac_relative.parts:
            raise ValueError
        dac_candidate = repo_root / dac_relative
        if dac_candidate.is_symlink():
            raise ValueError
        dac_path = dac_candidate.resolve(strict=False)
        dac_path.relative_to(repo_root.resolve())
        valid = valid and (
            dac_path.is_file()
            and dac_path.read_text(encoding="ascii") == "before-after.dac-proof.v1\n"
            and re.fullmatch(r"[0-9a-f]{64}", dac_sha256) is not None
            and hashlib.sha256(dac_path.read_bytes()).hexdigest() == dac_sha256
        )
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        return (
            {"status": "failed", "reason": "fixed gate metadata, commit, or JUnit output is invalid"},
            True,
        )
    return (
        {
            "status": "passed",
            "artifact": str(artifact.relative_to(repo_root.absolute())),
            "output_artifact": output_name,
            "output_sha256": output_sha256,
            "commit": value["commit"],
            "slice8_tests_executed": value["slice8_tests_executed"],
            "dac_proof_artifact": value["dac_proof_artifact"],
            "dac_proof_sha256": value["dac_proof_sha256"],
        },
        True,
    )


def generate(output: Path, repo_root: Path) -> int:
    checks = {name: _run(command, repo_root) for name, command in CHECK_COMMANDS.items()}
    fixed_gate, artifact_present = _fixed_gate_evidence(repo_root)
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
        commit = "unavailable"
    evidence = {
        "format": "before-after.local-evidence.v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "commit": commit,
        "python": platform.python_version(),
        "checks": checks,
        "fixed_gate": fixed_gate,
        "runtime_checks": "not_run" if fixed_gate["status"] != "passed" else "passed",
        "clinic_hardware_uat": "not_run",
        "clinic_tls_uat": "not_run",
        "clinic_permission_proof": "run deploy/verify-permissions.sh on the clinic host",
        "clinical_data": "not_collected",
    }
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="ascii")
    output.chmod(0o600)
    return 0 if all(item["passed"] for item in checks.values()) and (
        not artifact_present or fixed_gate["status"] == "passed"
    ) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/slice8-local-evidence.json"))
    args = parser.parse_args(argv)
    return generate(args.output, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    raise SystemExit(main())
