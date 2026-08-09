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


def _fixed_gate_evidence(repo_root: Path) -> tuple[dict[str, object], bool]:
    """Accept runtime success only with a completed, hash-bound fixed gate."""
    configured = os.environ.get("FIXED_GATE_ARTIFACT")
    artifact = Path(configured) if configured else repo_root / "artifacts/slice8-fixed-gate.json"
    if not artifact.is_absolute():
        artifact = repo_root / artifact
    artifact = artifact.resolve(strict=False)
    try:
        readable = artifact.is_file() and not artifact.is_symlink()
    except OSError:
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
        output_relative = Path(output_name)
        if not isinstance(output_name, str) or output_relative.is_absolute() or ".." in output_relative.parts:
            raise ValueError
        output = (repo_root / output_relative).resolve(strict=False)
        output.relative_to(repo_root)
        artifact.relative_to(repo_root)
        valid = (
            value.get("format") == "before-after.fixed-gate.v1"
            and value.get("completed_fixed_gate") is True
            and value.get("mandatory_skips") == 0
            and isinstance(value.get("slice8_tests_executed"), int)
            and value["slice8_tests_executed"] > 0
            and isinstance(output_name, str)
            and not output.is_symlink()
            and output.is_file()
            and isinstance(output_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", output_sha256) is not None
            and hashlib.sha256(output.read_bytes()).hexdigest() == output_sha256
        )
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        return ({"status": "failed", "reason": "fixed gate artifact is invalid or hash verification failed"}, True)
    return (
        {
            "status": "passed",
            "artifact": str(artifact.relative_to(repo_root)),
            "output_artifact": output_name,
            "output_sha256": output_sha256,
            "commit": value.get("commit", "unavailable"),
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
