#!/usr/bin/env python3
"""Fail-closed proof of the backup role's PostgreSQL database boundary."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any


ROLE = "before_after_backup"


class VerificationError(RuntimeError):
    pass


def _identifier(value: str, label: str) -> str:
    if not value or not value.isascii() or not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise VerificationError(f"{label} must be a PostgreSQL identifier")
    return value


def _password_file(path_value: str) -> tuple[Path, str]:
    path = Path(path_value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise VerificationError("backup password file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise VerificationError("backup password file must be a regular mode-0600 file")
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VerificationError("backup password file is unreadable") from exc
    if contents.endswith("\r\n"):
        password = contents[:-2]
    elif contents.endswith("\n"):
        password = contents[:-1]
    else:
        password = contents
    if not password or "\n" in password or "\r" in password:
        raise VerificationError("backup password file must contain one password line")
    return path, password


def _run(command: list[str], *, env: dict[str, str], label: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        raise VerificationError(f"{label} is unavailable") from exc
    return result


def _backup_environment(
    password: str,
    *,
    host: str,
    port: str,
) -> tuple[dict[str, str], Path]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PG") and key not in {"DATABASE_URL", "PGPASSWORD"}
    }
    try:
        descriptor, filename = tempfile.mkstemp(prefix="before-after-backup-hba-pgpass-")
        passfile = Path(filename)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(f"*:*:*:{ROLE}:{_pgpass_escape(password)}\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise VerificationError("could not create protected backup password file") from exc
    environment.update(
        {
            "PGUSER": ROLE,
            "PGHOST": host,
            "PGPORT": port,
            "PGPASSFILE": str(passfile),
        }
    )
    environment.pop("PGPASSWORD", None)
    return environment, passfile


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _run_psql(
    psql: str,
    database: str,
    *,
    env: dict[str, str],
    command: str,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            psql,
            "--no-psqlrc",
            "--no-password",
            "--dbname",
            database,
            "--command",
            command,
        ],
        env=env,
        label="psql",
    )


def _verify_hba_rules(
    psql: str,
    *,
    admin_database: str,
    admin_environment: dict[str, str],
    production_database: str,
    source_cidr: str,
) -> None:
    query = """
        SELECT COALESCE(
            json_agg(
                json_build_object(
                    'line_number', line_number,
                    'type', type,
                    'database', database,
                    'user_name', user_name,
                    'address', address,
                    'netmask', netmask,
                    'auth_method', auth_method,
                    'error', error
                ) ORDER BY line_number
            )::text,
            '[]'
        )
        FROM pg_hba_file_rules
    """
    result = _run(
        [
            psql,
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--no-password",
            "--dbname",
            admin_database,
            "--command",
            query,
        ],
        env=admin_environment,
        label="pg_hba_file_rules query",
    )
    if result.returncode != 0:
        raise VerificationError("pg_hba_file_rules could not be read")
    try:
        rules = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError as exc:
        raise VerificationError("pg_hba_file_rules returned invalid JSON") from exc
    if not isinstance(rules, list) or not rules:
        raise VerificationError("pg_hba_file_rules returned no rules")
    if any(rule.get("error") for rule in rules if isinstance(rule, dict)):
        raise VerificationError("pg_hba_file_rules contains an invalid rule")

    def values(rule: dict[str, Any], key: str) -> list[str]:
        value = rule.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        return []

    def relevant(rule: dict[str, Any], rule_type: str) -> bool:
        if rule_type == "local":
            type_matches = rule.get("type") == "local"
        else:
            type_matches = str(rule.get("type", "")).startswith("host")
        users = values(rule, "user_name")
        return type_matches and (ROLE in users or "all" in users)

    def is_allow(rule: dict[str, Any], rule_type: str, database: str) -> bool:
        return (
            relevant(rule, rule_type)
            and rule.get("type") == rule_type
            and values(rule, "database") == [database]
            and rule.get("auth_method") in {"scram-sha-256", "md5", "password"}
        )

    def is_reject(rule: dict[str, Any], rule_type: str) -> bool:
        return (
            relevant(rule, rule_type)
            and rule.get("type") == rule_type
            and ROLE in values(rule, "user_name")
            and values(rule, "database") == ["all"]
            and rule.get("auth_method") == "reject"
        )

    local_rules = [rule for rule in rules if isinstance(rule, dict) and relevant(rule, "local")]
    host_rules = [rule for rule in rules if isinstance(rule, dict) and relevant(rule, "host")]
    try:
        local_allow_index = next(index for index, rule in enumerate(local_rules) if is_allow(rule, "local", production_database))
        local_reject_index = next(index for index, rule in enumerate(local_rules) if is_reject(rule, "local"))
    except StopIteration as exc:
        raise VerificationError("HBA local allow/reject pair is missing") from exc
    if local_allow_index != 0 or local_reject_index != local_allow_index + 1:
        raise VerificationError("HBA local isolation rules are not first and adjacent")

    try:
        host_allow_index = next(index for index, rule in enumerate(host_rules) if is_allow(rule, "host", production_database))
    except StopIteration as exc:
        raise VerificationError("HBA host allow rule is missing") from exc
    host_allow = host_rules[host_allow_index]
    try:
        expected_network = ipaddress.ip_network(source_cidr, strict=True)
        actual_network = ipaddress.ip_network(
            f"{host_allow.get('address')}/{host_allow.get('netmask')}", strict=False
        )
    except (ValueError, TypeError) as exc:
        raise VerificationError("HBA host source is not a valid network") from exc
    if actual_network != expected_network:
        raise VerificationError("HBA host allow source is not the configured exact source")
    if host_allow_index != 0:
        raise VerificationError("HBA host allow rule is not before other role rules")

    reject4_index = next(
        (index for index, rule in enumerate(host_rules) if is_reject(rule, "host") and rule.get("address") == "0.0.0.0"),
        None,
    )
    reject6_index = next(
        (index for index, rule in enumerate(host_rules) if is_reject(rule, "host") and rule.get("address") == "::"),
        None,
    )
    if reject4_index != host_allow_index + 1 or reject6_index != host_allow_index + 2:
        raise VerificationError("HBA host isolation rejects are not immediate and ordered")


def verify(
    *,
    production_database: str,
    second_database: str,
    password_file: str,
    host: str,
    port: str,
    source_cidr: str,
    psql: str,
    pg_dump: str,
    admin_database: str,
    proof_file: str | None = None,
) -> None:
    production_database = _identifier(production_database, "production database")
    second_database = _identifier(second_database, "second database")
    if production_database == second_database:
        raise VerificationError("second database must differ from production database")
    admin_database = _identifier(admin_database, "administrator database")
    _password_path, password = _password_file(password_file)
    try:
        ipaddress.ip_network(source_cidr, strict=True)
    except ValueError as exc:
        raise VerificationError("backup service source must be an exact CIDR") from exc

    admin_environment = dict(os.environ)
    try:
        backup_environment, passfile = _backup_environment(password, host=host, port=port)
    except VerificationError:
        raise
    try:
        _verify_hba_rules(
            psql,
            admin_database=admin_database,
            admin_environment=admin_environment,
            production_database=production_database,
            source_cidr=source_cidr,
        )
        target = _run_psql(psql, production_database, env=backup_environment, command="SELECT 1")
        if target.returncode != 0:
            raise VerificationError("backup role could not connect to the production database")

        with tempfile.NamedTemporaryFile(prefix="before-after-hba-proof-", suffix=".dump", delete=False) as stream:
            dump_path = Path(stream.name)
        dump_path.chmod(0o600)
        try:
            dump = _run(
                [
                    pg_dump,
                    "--no-password",
                    "--format=custom",
                    "--file",
                    str(dump_path),
                    "--dbname",
                    production_database,
                ],
                env=backup_environment,
                label="pg_dump",
            )
            if dump.returncode != 0 or not dump_path.is_file() or dump_path.stat().st_size == 0:
                raise VerificationError("backup role pg_dump proof failed")
        finally:
            try:
                dump_path.unlink()
            except FileNotFoundError:
                pass

        other = _run_psql(psql, second_database, env=backup_environment, command="SELECT 1")
        if other.returncode == 0:
            raise VerificationError("backup role connected to a non-production database")

        if proof_file:
            proof_path = Path(proof_file)
            if proof_path.is_symlink():
                raise VerificationError("HBA proof path must not be a symlink")
            proof_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            proof_path.write_text(
                json.dumps(
                    {
                        "format": "before-after.backup-hba-proof.v1",
                        "production_database": production_database,
                        "second_database": second_database,
                        "hba_file_rules_verified": True,
                        "target_connection_succeeded": True,
                        "pg_dump_succeeded": True,
                        "second_database_rejected": True,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="ascii",
            )
            proof_path.chmod(0o600)
    finally:
        try:
            passfile.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-database", required=True)
    parser.add_argument("--second-database", required=True)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--host", default=os.environ.get("BACKUP_DB_HOST", "127.0.0.1"))
    parser.add_argument("--port", default=os.environ.get("BACKUP_DB_PORT", "5432"))
    parser.add_argument("--source-cidr", default=os.environ.get("BACKUP_HBA_SOURCE_CIDR", "127.0.0.1/32"))
    parser.add_argument("--admin-database", default=os.environ.get("BACKUP_HBA_ADMIN_DATABASE", "postgres"))
    parser.add_argument("--psql", default=os.environ.get("PSQL", "psql"))
    parser.add_argument("--pg-dump", default=os.environ.get("PG_DUMP", "pg_dump"))
    parser.add_argument("--proof-file", default=os.environ.get("BACKUP_HBA_PROOF_FILE"))
    args = parser.parse_args(argv)
    try:
        verify(
            production_database=args.production_database,
            second_database=args.second_database,
            password_file=args.password_file,
            host=args.host,
            port=args.port,
            source_cidr=args.source_cidr,
            psql=args.psql,
            pg_dump=args.pg_dump,
            admin_database=args.admin_database,
            proof_file=args.proof_file,
        )
    except VerificationError as exc:
        print(f"verify-postgres-backup-role: {exc}", file=sys.stderr)
        return 1
    print("verify-postgres-backup-role: HBA isolation, target pg_dump, and non-target denial passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
