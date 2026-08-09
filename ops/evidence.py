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
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from collections.abc import Iterator


REQUIRED_FIXED_GATE_TESTS = frozenset(
    {
        "test_actual_postgresql_backup_restore_and_authenticated_smoke",
        "test_backup_user_can_read_media_but_cannot_modify_or_delete",
    }
)
_FIXED_GATE_FIELDS = (
    "format",
    "completed_fixed_gate",
    "commit",
    "output_artifact",
    "output_sha256",
    "dac_proof_artifact",
    "dac_proof_sha256",
    "slice8_tests_executed",
    "mandatory_skips",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


CHECK_COMMANDS = {
    "shell_syntax": [
        "bash",
        "-c",
        'for script in "$@"; do bash -n "$script" || exit; done',
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
    if configured is not None and _COMMIT_RE.fullmatch(configured) is None:
        return None
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return configured
    if _COMMIT_RE.fullmatch(head) is None:
        return None
    if configured is not None and configured != head:
        return None
    return head


def _canonical_path(path: Path) -> Path:
    candidate = path.absolute()
    resolved = candidate.resolve(strict=False)
    if candidate != resolved:
        raise ValueError("evidence path contains a symlink component")
    return resolved


def _artifact_path(repo_root: Path) -> Path:
    configured = os.environ.get("FIXED_GATE_ARTIFACT")
    candidate = Path(configured) if configured else repo_root / "artifacts/slice8-fixed-gate.json"
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    artifact = _canonical_path(candidate)
    artifact.relative_to(repo_root.resolve(strict=False))
    return artifact


def _contained_member(value: object, evidence_root: Path, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"fixed gate {label} path is invalid")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"fixed gate {label} path escapes evidence directory")
    candidate = evidence_root / relative
    resolved = _canonical_path(candidate)
    resolved.relative_to(evidence_root.resolve(strict=False))
    return resolved


def _signature_path(artifact: Path, repo_root: Path) -> Path:
    configured = os.environ.get("EVIDENCE_SIGNATURE")
    candidate = Path(configured) if configured else artifact.with_suffix(artifact.suffix + ".sig")
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return _canonical_path(candidate)


@contextmanager
def _trusted_public_key(repo_root: Path) -> Iterator[Path | None]:
    """Yield only the configured key; artifact contents are never consulted."""
    configured = os.environ.get("EVIDENCE_PUBLIC_KEY")
    if not configured:
        yield None
        return
    if "-----BEGIN" in configured:
        temporary = tempfile.NamedTemporaryFile(mode="w", encoding="ascii", delete=False)
        try:
            temporary.write(configured)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o600)
            temporary.close()
            yield Path(temporary.name)
        finally:
            try:
                Path(temporary.name).unlink()
            except OSError:
                pass
        return
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    try:
        key = _canonical_path(candidate)
        if not key.is_file():
            raise ValueError("configured evidence public key is not a file")
    except (OSError, ValueError):
        yield None
        return
    yield key


def canonical_fixed_gate_metadata(metadata: dict[str, object]) -> bytes:
    """Return the exact ASCII bytes covered by a fixed-gate signature."""
    if not isinstance(metadata, dict):
        raise ValueError("fixed gate metadata is invalid")
    try:
        signed = {field: metadata[field] for field in _FIXED_GATE_FIELDS}
    except KeyError as exc:
        raise ValueError("fixed gate metadata is incomplete") from exc
    return json.dumps(signed, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _verify_signature(payload: bytes, signature: Path, repo_root: Path) -> tuple[bool, str]:
    if not signature.is_file() or signature.is_symlink():
        return False, "detached evidence signature is absent"
    try:
        if not signature.read_bytes():
            return False, "detached evidence signature is empty"
    except OSError:
        return False, "detached evidence signature is unreadable"
    with _trusted_public_key(repo_root) as public_key:
        if public_key is None:
            return False, "EVIDENCE_PUBLIC_KEY is not configured"
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            canonical_path = Path(stream.name)
        try:
            command = [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(public_key),
                "-signature",
                str(signature),
                str(canonical_path),
            ]
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError:
                return False, "openssl is unavailable"
            if result.returncode == 0:
                return True, ""
            # OpenSSL's dgst command handles RSA/EC keys. Ed25519 uses the
            # raw-message pkeyutl interface instead.
            try:
                result = subprocess.run(
                    [
                        "openssl",
                        "pkeyutl",
                        "-verify",
                        "-pubin",
                        "-inkey",
                        str(public_key),
                        "-sigfile",
                        str(signature),
                        "-in",
                        str(canonical_path),
                        "-rawin",
                    ],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError:
                return False, "openssl is unavailable"
            return (result.returncode == 0, "detached evidence signature is invalid")
        finally:
            try:
                canonical_path.unlink()
            except OSError:
                pass


def _junit_summary(path: Path) -> tuple[set[str], int, int, int, int]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError("fixed gate JUnit output is invalid") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("fixed gate JUnit root is invalid")
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("fixed gate JUnit output contains no test cases")
    identities: set[tuple[str, str]] = set()
    failures = errors = skipped = 0
    slice8_names: set[str] = set()
    slice8_count = 0
    for case in cases:
        classname = case.get("classname")
        name = case.get("name")
        if not isinstance(classname, str) or not classname or not isinstance(name, str) or not name:
            raise ValueError("fixed gate JUnit testcase identity is invalid")
        identity = (classname, name)
        if identity in identities:
            raise ValueError("fixed gate JUnit contains duplicate test cases")
        identities.add(identity)
        failures += case.find("failure") is not None
        errors += case.find("error") is not None
        skipped += case.find("skipped") is not None
        if classname.endswith("test_slice8"):
            slice8_count += 1
            slice8_names.add(name)
    suites = [root] if root.tag == "testsuite" else [root, *root.iter("testsuite")]
    for suite in suites:
        descendants = list(suite.iter("testcase"))
        raw_tests = suite.get("tests")
        if raw_tests is None:
            raise ValueError("fixed gate JUnit test count is missing")
        try:
            test_count = int(raw_tests)
        except (TypeError, ValueError) as exc:
            raise ValueError("fixed gate JUnit test count is invalid") from exc
        if test_count != len(descendants):
            raise ValueError("fixed gate JUnit test count is invalid")
        expected = {
            "failures": sum(case.find("failure") is not None for case in descendants),
            "errors": sum(case.find("error") is not None for case in descendants),
            "skipped": sum(case.find("skipped") is not None for case in descendants),
        }
        for field, count in expected.items():
            try:
                declared = int(suite.get(field, "0"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"fixed gate JUnit {field} count is invalid") from exc
            if declared != count:
                raise ValueError(f"fixed gate JUnit {field} count is invalid")
    return slice8_names, slice8_count, failures, errors, skipped


def _unverified(reason: str) -> dict[str, object]:
    return {"status": "unverified", "reason": reason}


def _fixed_gate_evidence(repo_root: Path) -> tuple[dict[str, object], bool]:
    """Accept runtime success only with signed, commit-bound fixed-gate data."""
    try:
        artifact = _artifact_path(repo_root)
    except (OSError, ValueError):
        return _unverified("fixed gate artifact path is invalid"), True
    if not artifact.is_file() or artifact.is_symlink():
        return (
            {
                "status": "not_run",
                "reason": "fixed PostgreSQL/DAC gate artifact is absent; syntax-only checks do not prove runtime",
            },
            False,
        )
    try:
        value = json.loads(artifact.read_text(encoding="ascii"))
        if not isinstance(value, dict):
            raise ValueError("fixed gate metadata is invalid")
        expected_commit = _repository_commit(repo_root)
        if expected_commit is None or value.get("commit") != expected_commit:
            raise ValueError("fixed gate commit does not match HEAD/BUILD_SHA")
        output = _contained_member(value.get("output_artifact"), artifact.parent, "JUnit")
        dac_path = _contained_member(value.get("dac_proof_artifact"), artifact.parent, "DAC proof")
        output_sha256 = value.get("output_sha256")
        dac_sha256 = value.get("dac_proof_sha256")
        if not isinstance(output_sha256, str) or _SHA256_RE.fullmatch(output_sha256) is None:
            raise ValueError("fixed gate JUnit hash is invalid")
        if not isinstance(dac_sha256, str) or _SHA256_RE.fullmatch(dac_sha256) is None:
            raise ValueError("fixed gate DAC hash is invalid")
        output_bytes = output.read_bytes()
        dac_bytes = dac_path.read_bytes()
        if hashlib.sha256(output_bytes).hexdigest() != output_sha256:
            raise ValueError("fixed gate JUnit hash does not match")
        if dac_bytes != b"before-after.dac-proof.v1\n" or hashlib.sha256(dac_bytes).hexdigest() != dac_sha256:
            raise ValueError("fixed gate DAC proof is invalid")
        slice8_names, slice8_count, failures, errors, skips = _junit_summary(output)
        if (
            value.get("format") != "before-after.fixed-gate.v1"
            or value.get("completed_fixed_gate") is not True
            or isinstance(value.get("mandatory_skips"), bool)
            or value.get("mandatory_skips") != 0
            or not isinstance(value.get("slice8_tests_executed"), int)
            or isinstance(value.get("slice8_tests_executed"), bool)
            or value["slice8_tests_executed"] != slice8_count
            or not REQUIRED_FIXED_GATE_TESTS <= slice8_names
            or failures != 0
            or errors != 0
            or skips != 0
        ):
            raise ValueError("fixed gate metadata or JUnit requirements are invalid")
        payload = canonical_fixed_gate_metadata(value)
        signature = _signature_path(artifact, repo_root)
        verified, reason = _verify_signature(payload, signature, repo_root)
        if not verified:
            return _unverified(reason), True
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return _unverified(str(exc)), True
    return (
        {
            "status": "passed",
            "artifact": str(artifact.relative_to(repo_root.resolve(strict=False))),
            "signature": str(signature.relative_to(repo_root.resolve(strict=False)))
            if signature.is_relative_to(repo_root.resolve(strict=False))
            else str(signature),
            "output_artifact": value["output_artifact"],
            "output_sha256": value["output_sha256"],
            "commit": value["commit"],
            "slice8_tests_executed": value["slice8_tests_executed"],
            "dac_proof_artifact": value["dac_proof_artifact"],
            "dac_proof_sha256": value["dac_proof_sha256"],
        },
        True,
    )


def generate(output: Path, repo_root: Path, *, certification: bool = False) -> int:
    checks = {name: _run(command, repo_root) for name, command in CHECK_COMMANDS.items()}
    fixed_gate, _ = _fixed_gate_evidence(repo_root)
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
        commit = os.environ.get("BUILD_SHA", "unavailable")
    runtime_passed = fixed_gate.get("status") == "passed"
    evidence = {
        "format": "before-after.local-evidence.v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "commit": commit,
        "python": platform.python_version(),
        "checks": checks,
        "fixed_gate": fixed_gate,
        "runtime_checks": "passed" if runtime_passed else "not_run",
        "clinic_hardware_uat": "not_run",
        "clinic_tls_uat": "not_run",
        "clinic_permission_proof": "run deploy/verify-permissions.sh on the clinic host",
        "clinical_data": "not_collected",
    }
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="ascii")
    output.chmod(0o600)
    checks_passed = all(item["passed"] for item in checks.values())
    return 0 if checks_passed and (not certification or runtime_passed) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/slice8-local-evidence.json"))
    parser.add_argument(
        "--certification",
        action="store_true",
        help="require a valid trusted signature before claiming runtime success",
    )
    args = parser.parse_args(argv)
    return generate(
        args.output,
        Path(__file__).resolve().parents[1],
        certification=args.certification or os.environ.get("EVIDENCE_CERTIFICATION") == "1",
    )


if __name__ == "__main__":
    raise SystemExit(main())
