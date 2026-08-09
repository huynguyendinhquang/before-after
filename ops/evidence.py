#!/usr/bin/env python3
"""Generate non-sensitive local evidence for a Slice 8 issue."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
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


def generate(output: Path, repo_root: Path) -> int:
    checks = {name: _run(command, repo_root) for name, command in CHECK_COMMANDS.items()}
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
        "clinic_hardware_uat": "not_run",
        "clinic_tls_uat": "not_run",
        "clinic_permission_proof": "run deploy/verify-permissions.sh on the clinic host",
        "clinical_data": "not_collected",
    }
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="ascii")
    output.chmod(0o600)
    return 0 if all(item["passed"] for item in checks.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/slice8-local-evidence.json"))
    args = parser.parse_args(argv)
    return generate(args.output, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    raise SystemExit(main())
