#!/usr/bin/env python3
"""Verify one backup generation and restore it into isolated targets."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from typing import Callable, Iterable, Iterator
from urllib.parse import unquote, urlsplit, urlunsplit
import uuid

from app.db import postgres_route, scoped_postgres_environment
from ops.backup import (
    DATABASE_DUMP_NAME,
    GENERATION_RE,
    MANIFEST_FORMAT_VERSION,
    MANIFEST_NAME,
    MEDIA_DIRS,
    POSTGRES_CLIENTS,
    OpsError,
    _assert_no_symlink_components,
    _fsync_directory,
    POSTGRES_DUMP_MAGIC,
    _native_postgres_url,
    _path,
    _path_overlap,
    _postgres_environment,
    postgres_preflight,
    _private_directory,
    _private_file,
    _run_native,
)


def _psycopg_connect(value: str, **overrides):
    """Connect with explicit canonical route settings, never ambient PG* state."""
    try:
        import psycopg
        parameters = postgres_route(value).psycopg_kwargs()
    except RuntimeError as exc:
        raise OpsError(str(exc)) from exc
    parameters.update(overrides)
    return psycopg.connect(**parameters)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')
_OWNER_MARKER_RE = re.compile(r"^before-after-restore-[0-9a-f]{32}$")
_RESTORE_STAGE_OWNER_NAME = ".restore-stage-owner.json"
_RESTORE_STAGE_OWNER_FORMAT = "before-after.restore-stage.v1"
_RESTORE_STAGE_RE = re.compile(r"^\.(?:restore-source|restore)-([0-9a-f]{32})$")
_RESTORE_STAGING_LOCK_NAME = ".restore-staging.lock"
_RESTORE_REGISTRY_FORMAT = "before-after.restore-run.v1"
_RESTORE_REGISTRY_RE = re.compile(r"^\.restore-run-([0-9a-f]{32})\.json$")
_GENERATED_DATABASE_RE = re.compile(r"^before_after_restore_[0-9a-f]{32}$")
_RESTORE_STAGING_OWNER_NAME = ".staging-owner.json"
_SMOKE_ACCOUNT_DISPLAY_NAME = "Temporary restore smoke account"


@dataclass(frozen=True)
class VerifiedGeneration:
    name: str
    path: Path
    manifest: dict[str, object]
    files: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class RestoreResult:
    generation: str
    media_files: int
    migration_checked: bool
    database_media_checked: bool
    smoke_checked: bool


def _target_identity(parent: Path, target: Path) -> str:
    del parent
    return target.resolve(strict=False).as_posix()


def _owner_marker_path(parent: Path, target: Path) -> Path:
    identity = _target_identity(parent, target).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return parent / f".restore-owner-{digest}"


def _write_owner_marker(parent: Path, target: Path, marker: str) -> None:
    if _OWNER_MARKER_RE.fullmatch(marker) is None:
        raise OpsError("restore ownership marker is invalid")
    path = _owner_marker_path(parent, target)
    _assert_no_symlink_components(path)
    try:
        with path.open("x", encoding="ascii") as stream:
            stream.write(marker + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
        _fsync_directory(parent)
    except FileExistsError as exc:
        raise OpsError("restore ownership marker already exists") from exc
    except OSError as exc:
        raise OpsError("could not record restore ownership marker") from exc


def _read_owner_marker(parent: Path, target: Path) -> str:
    path = _owner_marker_path(parent, target)
    _private_file(path, label="restore ownership marker")
    try:
        marker = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise OpsError("could not read restore ownership marker") from exc
    if _OWNER_MARKER_RE.fullmatch(marker) is None:
        raise OpsError("restore ownership marker is invalid")
    return marker


def _ensure_owner_marker(parent: Path, target: Path, marker: str) -> None:
    path = _owner_marker_path(parent, target)
    if path.exists() or os.path.lexists(path):
        if _read_owner_marker(parent, target) != marker:
            raise OpsError("restore ownership marker does not match this run")
        return
    _write_owner_marker(parent, target, marker)


def _write_json_atomic(path: Path, value: dict[str, object], *, replace: bool = True) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="ascii") as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        if not replace and (path.exists() or os.path.lexists(path)):
            raise OpsError("restore run registry marker already exists")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OpsError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise OpsError("could not write restore run registry marker") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


@contextmanager
def _restore_staging_lock(parent: Path) -> Iterator[None]:
    lock_path = parent / _RESTORE_STAGING_LOCK_NAME
    descriptor: int | None = None
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OpsError("restore staging lock is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OpsError:
        raise
    except OSError as exc:
        raise OpsError("could not acquire restore staging lock") from exc
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _write_restore_stage_owner(
    staging: Path,
    *,
    parent: Path,
    target: Path | None,
    run_id: str,
    kind: str,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise OpsError("restore staging run identifier is invalid")
    payload = {
        "format": _RESTORE_STAGE_OWNER_FORMAT,
        "stage": staging.name,
        "kind": kind,
        "run_id": run_id,
        "target": _target_identity(parent, target) if target is not None else None,
        "pid": os.getpid(),
        "created_at": time.time(),
    }
    temporary = staging / f".{_RESTORE_STAGE_OWNER_NAME}.{uuid.uuid4().hex}.tmp"
    owner = staging / _RESTORE_STAGE_OWNER_NAME
    try:
        with temporary.open("x", encoding="ascii") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, owner)
        os.chmod(owner, 0o600)
        _fsync_directory(staging)
    except (OSError, TypeError, ValueError) as exc:
        raise OpsError("could not record restore staging ownership") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _remove_restore_stage_owner(staging: Path) -> None:
    try:
        (staging / _RESTORE_STAGE_OWNER_NAME).unlink()
    except FileNotFoundError:
        raise OpsError("restore staging ownership metadata is missing")
    except OSError as exc:
        raise OpsError("could not remove restore staging ownership metadata") from exc
    _fsync_directory(staging)


def _restore_run_id(value: str) -> str:
    if _OWNER_MARKER_RE.fullmatch(value):
        return value.rsplit("-", 1)[1]
    if re.fullmatch(r"[0-9a-f]{32}", value):
        return value
    raise OpsError("restore staging run identifier is invalid")


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _restore_stage_is_owned_stale(
    entry: Path,
    *,
    parent: Path,
    target: Path,
    run_id: str,
    now: float,
    age_seconds: float,
) -> bool:
    match = _RESTORE_STAGE_RE.fullmatch(entry.name)
    if match is None or entry.is_symlink() or not entry.is_dir():
        return False
    owner = entry / _RESTORE_STAGE_OWNER_NAME
    try:
        _private_file(owner, label="restore staging owner metadata")
        metadata = json.loads(owner.read_text(encoding="ascii"))
        age = now - entry.stat().st_mtime
    except (OpsError, OSError, UnicodeError, ValueError, TypeError):
        return False
    return (
        age >= age_seconds
        and isinstance(metadata, dict)
        and metadata.get("format") == _RESTORE_STAGE_OWNER_FORMAT
        and metadata.get("stage") == entry.name
        and metadata.get("run_id") == run_id
        and metadata.get("target") == _target_identity(parent, target)
        and metadata.get("kind") in {"source", "media"}
        and isinstance(metadata.get("created_at"), (int, float))
        and not isinstance(metadata.get("created_at"), bool)
        and metadata["created_at"] <= now
        and isinstance(metadata.get("pid"), int)
        and not isinstance(metadata.get("pid"), bool)
        and not _process_is_alive(metadata["pid"])
        and match.group(1)
        and metadata.get("kind") == ("source" if entry.name.startswith(".restore-source-") else "media")
    )


def cleanup_stale_restore_staging(
    *,
    restore_parent: str | os.PathLike[str],
    target_media_root: str | os.PathLike[str],
    run_id: str,
    age_seconds: float = 24 * 60 * 60,
    now: float | None = None,
) -> list[str]:
    """Delete only old, metadata-owned restore stages while holding the lock."""
    if isinstance(age_seconds, bool) or not isinstance(age_seconds, (int, float)) or age_seconds < 0:
        raise OpsError("stale restore staging age must be non-negative")
    parent = _private_directory(_path(restore_parent, "restore parent"), create=False)
    target = _path(target_media_root, "restore MEDIA_ROOT")
    normalized_run_id = _restore_run_id(run_id)
    current_time = time.time() if now is None else now
    removed: list[str] = []
    with _restore_staging_lock(parent):
        try:
            entries = list(parent.iterdir())
        except OSError as exc:
            raise OpsError("could not list restore staging") from exc
        for entry in entries:
            if not _restore_stage_is_owned_stale(
                entry,
                parent=parent,
                target=target,
                run_id=normalized_run_id,
                now=current_time,
                age_seconds=float(age_seconds),
            ):
                continue
            _remove_private_path_strict(entry)
            removed.append(entry.name)
        if removed:
            _fsync_directory(parent)
    return removed


def _safe_manifest_path(value: object) -> str:
    if not isinstance(value, str):
        raise OpsError("backup manifest path is invalid")
    pure = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise OpsError("backup manifest path is unsafe")
    return value


def _sha256_file(path: Path) -> tuple[int, str]:
    _private_file(path, label="backup file")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise OpsError("could not checksum backup file") from exc
    return size, digest.hexdigest()


def _load_json(path: Path) -> dict[str, object]:
    _private_file(path, label="backup manifest")
    try:
        with path.open(encoding="ascii") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise OpsError("backup manifest is invalid") from exc
    if not isinstance(value, dict):
        raise OpsError("backup manifest is invalid")
    return value


def _walk_files(root: Path) -> set[str]:
    result: set[str] = set()
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        _private_directory(current_path, create=False)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise OpsError("symlinks are not allowed in backup generations")
            _private_directory(path, create=False)
        for name in files:
            path = current_path / name
            if path.is_symlink():
                raise OpsError("symlinks are not allowed in backup generations")
            _private_file(path, label="backup file")
            result.add(path.relative_to(root).as_posix())
    return result


def verify_generation(
    path: str | os.PathLike[str],
    *,
    expected_name: str | None = None,
    allow_staging_owner: bool = False,
) -> VerifiedGeneration:
    """Verify manifest, checksums, modes, paths, and absence of partial files."""
    generation_path = _path(path, "backup generation")
    _assert_no_symlink_components(generation_path, allow_missing_leaf=False)
    published_name = generation_path.name if expected_name is None else expected_name
    if GENERATION_RE.fullmatch(published_name) is None:
        raise OpsError("backup generation name is invalid")
    if published_name != generation_path.name and not allow_staging_owner:
        raise OpsError("backup generation name is invalid")
    _private_directory(generation_path, create=False)
    manifest = _load_json(generation_path / MANIFEST_NAME)
    if manifest.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise OpsError("backup manifest format is unsupported")
    if manifest.get("complete") is not True or manifest.get("generation") != published_name:
        raise OpsError("backup generation is incomplete")
    database = manifest.get("database")
    media = manifest.get("media")
    postgres = manifest.get("postgresql")
    raw_files = manifest.get("files")
    if not isinstance(database, dict) or database.get("path") != DATABASE_DUMP_NAME or database.get("format") != "custom":
        raise OpsError("backup database entry is invalid")
    if not isinstance(media, dict) or media.get("path") != "media":
        raise OpsError("backup media entry is invalid")
    if not isinstance(postgres, dict):
        raise OpsError("backup PostgreSQL preflight entry is missing")
    if postgres.get("test_only") is not True:
        server_major = postgres.get("server_major")
        client_majors = postgres.get("client_majors")
        if (
            isinstance(server_major, bool)
            or not isinstance(server_major, int)
            or not 9 <= server_major <= 99
            or not isinstance(client_majors, dict)
            or set(client_majors) != set(POSTGRES_CLIENTS)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != server_major
                for value in client_majors.values()
            )
        ):
            raise OpsError("backup PostgreSQL preflight entry is invalid")
    if media.get("directories") != list(MEDIA_DIRS):
        raise OpsError("backup media directories are invalid")
    if not isinstance(raw_files, list) or not raw_files:
        raise OpsError("backup manifest has no files")

    entries: list[dict[str, object]] = []
    paths: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise OpsError("backup manifest file entry is invalid")
        relative = _safe_manifest_path(raw.get("path"))
        if relative in paths:
            raise OpsError("backup manifest contains duplicate files")
        size = raw.get("bytes")
        digest = raw.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise OpsError("backup manifest file size is invalid")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise OpsError("backup manifest checksum is invalid")
        if relative != DATABASE_DUMP_NAME and not relative.startswith("media/"):
            raise OpsError("backup manifest contains an unexpected file")
        if relative.startswith("media/"):
            parts = PurePosixPath(relative).parts
            if len(parts) < 3 or parts[1] not in MEDIA_DIRS:
                raise OpsError("backup manifest media path is invalid")
        file_path = generation_path / Path(*PurePosixPath(relative).parts)
        _assert_no_symlink_components(file_path, allow_missing_leaf=False)
        actual_size, actual_digest = _sha256_file(file_path)
        if actual_size != size or actual_digest != digest:
            raise OpsError("backup generation checksum verification failed")
        paths.add(relative)
        entries.append({"path": relative, "bytes": size, "sha256": digest})

    if DATABASE_DUMP_NAME not in paths:
        raise OpsError("backup generation has no PostgreSQL dump")
    actual_paths = _walk_files(generation_path)
    actual_paths.discard(MANIFEST_NAME)
    if allow_staging_owner:
        _private_file(generation_path / _RESTORE_STAGING_OWNER_NAME, label="backup staging owner marker")
        actual_paths.discard(_RESTORE_STAGING_OWNER_NAME)
    if actual_paths != paths:
        raise OpsError("backup generation contains partial or unlisted files")
    for directory_name in MEDIA_DIRS:
        media_directory = generation_path / "media" / directory_name
        _private_directory(media_directory, create=False)
    return VerifiedGeneration(
        name=published_name,
        path=generation_path,
        manifest=manifest,
        files=tuple(sorted(entries, key=lambda item: str(item["path"]))),
    )


def select_generation(
    backup_root: str | os.PathLike[str],
    generation: str | None = None,
) -> VerifiedGeneration:
    root = _private_directory(_path(backup_root, "BACKUP_ROOT"), create=False)
    if generation is not None:
        if not isinstance(generation, str) or "/" in generation or "\\" in generation or ".." in generation:
            raise OpsError("backup generation name is unsafe")
        return verify_generation(root / generation)
    try:
        candidates = sorted(
            entry
            for entry in root.iterdir()
            if GENERATION_RE.fullmatch(entry.name) and not entry.is_symlink()
        )
    except OSError as exc:
        raise OpsError("could not list backup generations") from exc
    if not candidates:
        raise OpsError("no complete backup generations were found")
    # Unsupported or corrupt generations are preserved but never become a
    # restore candidate.  The verifier is the same one used by retention.
    verified: list[VerifiedGeneration] = []
    for entry in candidates:
        try:
            verified.append(verify_generation(entry))
        except (OpsError, OSError, UnicodeError, ValueError, TypeError):
            continue
    if not verified:
        raise OpsError("no restorable v2 backup generations were found")
    return verified[-1]


def postgres_restore_preflight(
    generation: VerifiedGeneration,
    target_database_url: str,
    *,
    pg_dump: str = "pg_dump",
    pg_restore: str = "pg_restore",
    psql: str = "psql",
    source_database_url: str | None = None,
) -> dict[str, object] | None:
    """Reject a restore when its native clients or target major differ."""
    metadata = generation.manifest.get("postgresql")
    if not isinstance(metadata, dict):
        raise OpsError("backup PostgreSQL preflight entry is missing")
    if metadata.get("test_only") is True:
        return None
    expected = metadata.get("server_major")
    if isinstance(expected, bool) or not isinstance(expected, int):
        raise OpsError("backup PostgreSQL server major is invalid")
    target = postgres_preflight(
        target_database_url,
        pg_dump=pg_dump,
        pg_restore=pg_restore,
        psql=psql,
    )
    if target.get("server_major") != expected:
        raise OpsError(
            f"PostgreSQL restore target major {target.get('server_major')} "
            f"does not match backup major {expected}"
        )
    if source_database_url is not None:
        source = postgres_preflight(
            source_database_url,
            pg_dump=pg_dump,
            pg_restore=pg_restore,
            psql=psql,
        )
        if source.get("server_major") != expected:
            raise OpsError("PostgreSQL source major does not match backup manifest")
    return target


def _normalize_server_address(value: object) -> str | None:
    if value is None:
        return None
    try:
        import ipaddress

        return str(ipaddress.ip_address(str(value).split("/", 1)[0]))
    except ValueError as exc:
        raise OpsError("PostgreSQL server address is invalid") from exc


def _normalize_server_port(value: object) -> int | None:
    if value is None:
        return None
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise OpsError("PostgreSQL server port is invalid") from exc
    if not 1 <= port <= 65535:
        raise OpsError("PostgreSQL server port is invalid")
    return port


def _system_identifier(cursor) -> str:
    try:
        cursor.execute("SELECT system_identifier::text FROM pg_control_system()")
        row = cursor.fetchone()
    except Exception as exc:
        raise OpsError("PostgreSQL cluster system identifier is unavailable") from exc
    if not row or not row[0]:
        raise OpsError("PostgreSQL cluster system identifier is unavailable")
    return str(row[0])


def _connection_database_identity(value: str) -> tuple[object, ...]:
    """Resolve a PostgreSQL URL to cluster/database identity, not URL spelling."""
    try:
        import psycopg
    except ImportError as exc:
        raise OpsError("psycopg is required to establish production database identity") from exc
    try:
        with scoped_postgres_environment(value), _psycopg_connect(value, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT inet_server_addr()::text, inet_server_port(), current_database(), "
                    "(SELECT oid FROM pg_database WHERE datname = current_database())"
                )
                row = tuple(cursor.fetchone() or ())
                address, port, database, database_oid = (row + (None, None, None, None))[:4]
                system_identifier = _system_identifier(cursor)
    except OpsError:
        raise
    except Exception as exc:
        raise OpsError("could not establish PostgreSQL connection identity") from exc
    if not isinstance(database, str) or not database:
        raise OpsError("PostgreSQL connection database name is unavailable")
    if isinstance(database_oid, bool) or database_oid is None:
        raise OpsError("PostgreSQL connection database OID is unavailable")
    try:
        database_oid = int(database_oid)
    except (TypeError, ValueError) as exc:
        raise OpsError("PostgreSQL connection database OID is invalid") from exc
    if database_oid <= 0:
        raise OpsError("PostgreSQL connection database OID is invalid")
    # inet_server_addr()/inet_server_port() are NULL for Unix sockets. They
    # are useful diagnostics, but the cluster identifier plus database OID is
    # the verified identity used for safety decisions.
    return (
        "cluster",
        system_identifier,
        database_oid,
        database,
        _normalize_server_address(address),
        _normalize_server_port(port),
    )


def _connection_server_identity(value: str) -> tuple[object, ...]:
    """Resolve a PostgreSQL URL to cluster identity without opening its DB."""
    try:
        import psycopg
    except ImportError as exc:
        raise OpsError("psycopg is required to establish PostgreSQL server identity") from exc
    try:
        with scoped_postgres_environment(value), _psycopg_connect(_maintenance_database_url(value), connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT inet_server_addr()::text, inet_server_port()")
                address, port = (cursor.fetchone() or (None, None))[:2]
                system_identifier = _system_identifier(cursor)
    except OpsError:
        raise
    except Exception as exc:
        raise OpsError("could not establish PostgreSQL server identity") from exc
    return (
        "cluster",
        system_identifier,
        _normalize_server_address(address),
        _normalize_server_port(port),
    )


def _same_cluster(server_identity: tuple[object, ...], database_identity: tuple[object, ...]) -> bool:
    return (
        len(server_identity) >= 2
        and len(database_identity) >= 2
        and server_identity[:2] == database_identity[:2]
    )


def _same_database(
    first: tuple[object, ...], second: tuple[object, ...]
) -> bool:
    return len(first) >= 3 and len(second) >= 3 and first[:3] == second[:3]


def _registry_path(parent: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise OpsError("restore run identifier is invalid")
    return parent / f".restore-run-{run_id}.json"


def _registry_value(path: Path) -> dict[str, object]:
    _private_file(path, label="restore run registry marker")
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise OpsError("restore run registry marker is invalid") from exc
    if not isinstance(value, dict) or value.get("format") != _RESTORE_REGISTRY_FORMAT:
        raise OpsError("restore run registry marker is invalid")
    return value


def _database_state(target_database_url: str) -> tuple[bool, str | None, bool]:
    """Return existence, exact comment, and whether the user database is empty."""
    try:
        import psycopg
    except ImportError as exc:
        raise OpsError("psycopg is required for restore run recovery") from exc
    target_name = _database_name(target_database_url)
    try:
        with scoped_postgres_environment(target_database_url), _psycopg_connect(_maintenance_database_url(target_database_url), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
                    (target_name,),
                )
                row = cursor.fetchone()
                if row is None:
                    return False, None, True
                comment = row[0]
        with scoped_postgres_environment(target_database_url), _psycopg_connect(target_database_url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT "
                    "NOT EXISTS (SELECT 1 FROM pg_namespace "
                    "WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'public') "
                    "AND nspname NOT LIKE 'pg_toast%' "
                    "AND nspname NOT LIKE 'pg_temp_%' "
                    "AND nspname NOT LIKE 'pg_toast_temp_%'), "
                    "NOT EXISTS (SELECT 1 FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                    "AND n.nspname NOT LIKE 'pg_toast%' "
                    "AND n.nspname NOT LIKE 'pg_temp_%' "
                    "AND n.nspname NOT LIKE 'pg_toast_temp_%'), "
                    "NOT EXISTS (SELECT 1 FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                    "AND n.nspname NOT LIKE 'pg_toast%' "
                    "AND n.nspname NOT LIKE 'pg_temp_%' "
                    "AND n.nspname NOT LIKE 'pg_toast_temp_%'), "
                    "NOT EXISTS (SELECT 1 FROM pg_type t "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                    "AND n.nspname NOT LIKE 'pg_toast%' "
                    "AND n.nspname NOT LIKE 'pg_temp_%' "
                    "AND n.nspname NOT LIKE 'pg_toast_temp_%'), "
                    "NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql'), "
                    "NOT EXISTS (SELECT 1 FROM pg_language "
                    "WHERE lanname NOT IN ('internal', 'c', 'sql', 'plpgsql')), "
                    "NOT EXISTS (SELECT 1 FROM pg_foreign_data_wrapper), "
                    "NOT EXISTS (SELECT 1 FROM pg_foreign_server), "
                    "NOT EXISTS (SELECT 1 FROM pg_operator o "
                    "JOIN pg_namespace n ON n.oid = o.oprnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')), "
                    "NOT EXISTS (SELECT 1 FROM pg_event_trigger), "
                    "NOT EXISTS (SELECT 1 FROM pg_publication), "
                    "NOT EXISTS (SELECT 1 FROM pg_subscription), "
                    "NOT EXISTS (SELECT 1 FROM pg_replication_origin), "
                    "NOT EXISTS (SELECT 1 FROM pg_default_acl), "
                    "NOT EXISTS (SELECT 1 FROM pg_collation "
                    "WHERE collnamespace NOT IN (SELECT oid FROM pg_namespace "
                    "WHERE nspname IN ('pg_catalog', 'information_schema'))), "
                    "NOT EXISTS (SELECT 1 FROM pg_conversion "
                    "WHERE connamespace NOT IN (SELECT oid FROM pg_namespace "
                    "WHERE nspname IN ('pg_catalog', 'information_schema'))), "
                    "NOT EXISTS (SELECT 1 FROM pg_largeobject_metadata)"
                )
                pristine = tuple(cursor.fetchone() or ())
                # Keep this count equal to the SELECT list: a missing check
                # must never turn a non-pristine catalog into an empty one.
                empty = len(pristine) == 17 and all(value is True for value in pristine)
    except OpsError:
        raise
    except Exception as exc:
        raise OpsError("could not prove restore database state; manual empty-DB cleanup is required") from exc
    return True, comment, empty


def _drop_registry_database(target_database_url: str, *, expected_comment: str) -> None:
    if _OWNER_MARKER_RE.fullmatch(expected_comment) is None:
        raise OpsError("restore database ownership comment is required; manual cleanup is required")
    try:
        import psycopg
    except ImportError as exc:
        raise OpsError("psycopg is required to recover the restore database") from exc
    target_name = _database_name(target_database_url)
    try:
        with scoped_postgres_environment(target_database_url), _psycopg_connect(_maintenance_database_url(target_database_url), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
                    (target_name,),
                )
                row = cursor.fetchone()
                if row is None or row[0] != expected_comment:
                    raise OpsError("restore database ownership proof changed; manual cleanup is required")
                cursor.execute(f'DROP DATABASE "{target_name}" WITH (FORCE)')
    except OpsError:
        raise
    except Exception as exc:
        raise OpsError("could not recover restore database; manual empty-DB cleanup is required") from exc


def _adopt_recovered_media(parent: Path, target: Path, marker: str) -> None:
    """Prove ownership before adopting a target left by this run."""
    _assert_no_symlink_components(target)
    try:
        target.relative_to(parent)
    except ValueError as exc:
        raise OpsError("restore target must be inside the private restore parent") from exc
    if target == parent:
        raise OpsError("restore target must be inside the private restore parent")
    if target.exists() or os.path.lexists(target):
        _private_directory(target, create=False)
        try:
            mode = stat.S_IMODE(os.stat(target, follow_symlinks=False).st_mode)
            with os.scandir(target) as entries:
                if next(entries, None) is not None:
                    raise OpsError(
                        "restore MEDIA_ROOT is not empty; manual cleanup is required"
                    )
        except OpsError:
            raise
        except OSError as exc:
            raise OpsError("could not inspect restore MEDIA_ROOT") from exc
        if mode != 0o700:
            raise OpsError("restore MEDIA_ROOT is not private")
        if _read_owner_marker(parent, target) != marker:
            raise OpsError("restore ownership marker does not match this run")
        return
    _ensure_owner_marker(parent, target, marker)


def recover_provisioning(
    *,
    target_database_url: str,
    restore_parent: str | os.PathLike[str],
    target_media_root: str | os.PathLike[str],
) -> str | None:
    """Recover only a registry-proven target left across a provisioning crash."""
    parent = _private_directory(_path(restore_parent, "restore parent"), create=False)
    target = _path(target_media_root, "restore MEDIA_ROOT")
    _assert_no_symlink_components(target)
    try:
        target.relative_to(parent)
    except ValueError as exc:
        raise OpsError("restore target must be inside the private restore parent") from exc
    if target == parent:
        raise OpsError("restore target must be inside the private restore parent")
    target_name = _database_name(target_database_url)
    server: tuple[object, ...] | None = None
    with _restore_staging_lock(parent):
        try:
            entries = list(parent.iterdir())
        except OSError as exc:
            raise OpsError("could not list restore run registry") from exc
        for entry in entries:
            match = _RESTORE_REGISTRY_RE.fullmatch(entry.name)
            if match is None or entry.is_symlink():
                continue
            value = _registry_value(entry)
            if value.get("run_id") != match.group(1):
                raise OpsError("restore run registry marker has inconsistent run identifier")
            if value.get("database_name") != target_name:
                continue
            if server is None:
                try:
                    server = _connection_server_identity(target_database_url)
                except OpsError as exc:
                    raise OpsError(
                        "could not prove restore provisioning ownership; "
                        "manual empty-DB cleanup is required"
                    ) from exc
            stored_server = value.get("server_identity")
            if (
                not isinstance(stored_server, list)
                or not _same_cluster(tuple(stored_server), server)
            ):
                raise OpsError("restore run registry server identity does not match")
            if value.get("target_media_identity") != _target_identity(parent, target):
                raise OpsError("restore run registry target identity does not match")
            marker = value.get("ownership_marker")
            if not isinstance(marker, str) or _OWNER_MARKER_RE.fullmatch(marker) is None:
                raise OpsError("restore run registry ownership marker is invalid")
            state = value.get("state")
            if state not in {"planned", "created", "owned"}:
                raise OpsError("restore run registry state is invalid")
            exists, comment, empty = _database_state(target_database_url)
            if not exists:
                entry.unlink()
                _fsync_directory(parent)
                continue
            if comment == marker:
                if empty is not True:
                    raise OpsError(
                        "restore database is not pristine; manual empty-DB cleanup is required"
                    )
                _adopt_recovered_media(parent, target, marker)
                value["state"] = "owned"
                _write_json_atomic(entry, value)
                entry.unlink()
                _fsync_directory(parent)
                return marker
            if comment is None:
                raise OpsError(
                    "restore database ownership comment is absent; "
                    "manual empty-DB cleanup is required"
                )
            raise OpsError(
                "restore provisioning ownership cannot be proven; "
                "manual empty-DB cleanup is required"
            )
        owner_path = _owner_marker_path(parent, target)
        if target.exists() or os.path.lexists(target) or owner_path.exists() or os.path.lexists(owner_path):
            if not owner_path.exists() and not os.path.lexists(owner_path):
                raise OpsError(
                    "restore MEDIA_ROOT ownership marker is missing; manual cleanup is required"
                )
            marker = _read_owner_marker(parent, target)
            exists, comment, empty = _database_state(target_database_url)
            if not exists or comment != marker:
                raise OpsError(
                    "restore provisioning ownership cannot be proven; "
                    "manual empty-DB cleanup is required"
                )
            if empty is not True:
                raise OpsError(
                    "restore database is not pristine; manual empty-DB cleanup is required"
                )
            _adopt_recovered_media(parent, target, marker)
            return marker
    return None


def _database_name(value: str) -> str:
    parsed = urlsplit(_native_postgres_url(value))
    name = unquote(parsed.path.lstrip("/"))
    if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name):
        raise OpsError("PostgreSQL target database name is invalid")
    return name


def create_isolated_database(
    target_database_url: str,
    production_database_urls: Iterable[str],
    *,
    restore_parent: str | os.PathLike[str] | None = None,
    target_media_root: str | os.PathLike[str] | None = None,
) -> str:
    if restore_parent is None or target_media_root is None:
        raise OpsError("restore parent and target media are required before target creation")
    parent = _private_directory(_path(restore_parent, "restore parent"), create=False)
    with _restore_staging_lock(parent):
        return _create_isolated_database(
            target_database_url,
            production_database_urls,
            restore_parent=parent,
            target_media_root=target_media_root,
        )


def _create_isolated_database(
    target_database_url: str,
    production_database_urls: Iterable[str],
    *,
    restore_parent: str | os.PathLike[str] | None = None,
    target_media_root: str | os.PathLike[str] | None = None,
) -> str:
    """Create and mark a new target DB through a mandatory admin connection."""
    production_urls = tuple(
        value for value in production_database_urls if isinstance(value, str) and value.strip()
    )
    if not production_urls:
        raise OpsError("a production database identifier is required before target creation")
    if restore_parent is None or target_media_root is None:
        raise OpsError("restore parent and target media are required before target creation")
    parent = _private_directory(_path(restore_parent, "restore parent"), create=False)
    target = _path(target_media_root, "restore MEDIA_ROOT")
    _assert_no_symlink_components(target)
    try:
        target.relative_to(parent)
    except ValueError as exc:
        raise OpsError("restore target must be inside the private restore parent") from exc
    if target == parent or target.exists() or os.path.lexists(target):
        raise OpsError("restore MEDIA_ROOT must be a new isolated target")
    target_server = _connection_server_identity(target_database_url)
    target_name = _database_name(target_database_url)
    if _GENERATED_DATABASE_RE.fullmatch(target_name) is None:
        raise OpsError("restore database name must be generated and collision-resistant")
    for production_url in production_urls:
        production_identity = _connection_database_identity(production_url)
        if _same_cluster(target_server, production_identity) and target_name == production_identity[3]:
            raise OpsError("restore target database is a production database")
    try:
        import psycopg
    except ImportError as exc:
        raise OpsError("psycopg is required to create the restore database") from exc
    marker = f"before-after-restore-{uuid.uuid4().hex}"
    run_id = marker.rsplit("-", 1)[1]
    registry_path = _registry_path(parent, run_id)
    registry: dict[str, object] = {
        "format": _RESTORE_REGISTRY_FORMAT,
        "run_id": run_id,
        "database_name": target_name,
        "server_identity": list(target_server),
        "ownership_marker": marker,
        "target_media_identity": _target_identity(parent, target),
        "state": "planned",
        "created_at": time.time(),
    }
    _write_json_atomic(registry_path, registry, replace=False)
    created = False
    commented = False
    try:
        with scoped_postgres_environment(target_database_url), _psycopg_connect(_maintenance_database_url(target_database_url), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_name,))
                if cursor.fetchone() is not None:
                    raise OpsError("restore target database already exists; choose a new isolated name")
                cursor.execute(f'CREATE DATABASE "{target_name}"')
                created = True
                registry["state"] = "created"
                _write_json_atomic(registry_path, registry)
                cursor.execute(f"COMMENT ON DATABASE \"{target_name}\" IS '{marker}'")
                commented = True
        _write_owner_marker(parent, target, marker)
        registry["state"] = "owned"
        _write_json_atomic(registry_path, registry)
        registry_path.unlink()
        _fsync_directory(parent)
        return marker
    except OpsError:
        if created:
            if not commented:
                raise OpsError(
                    "restore database ownership comment was not written; "
                    "manual empty-DB cleanup is required"
                )
            try:
                _drop_registry_database(target_database_url, expected_comment=marker)
            except Exception as rollback_exc:
                raise OpsError("restore database provisioning failed; target is poisoned") from rollback_exc
        try:
            registry_path.unlink()
            _fsync_directory(parent)
        except FileNotFoundError:
            pass
        except OSError as registry_exc:
            raise OpsError("restore database provisioning failed; run registry is poisoned") from registry_exc
        raise
    except Exception as exc:
        if created:
            if not commented:
                raise OpsError(
                    "restore database ownership comment was not written; "
                    "manual empty-DB cleanup is required"
                ) from exc
            try:
                _drop_registry_database(target_database_url, expected_comment=marker)
            except Exception as rollback_exc:
                raise OpsError("restore database provisioning failed; target is poisoned") from rollback_exc
        try:
            registry_path.unlink()
            _fsync_directory(parent)
        except FileNotFoundError:
            pass
        except OSError as registry_exc:
            raise OpsError("restore database provisioning failed; run registry is poisoned") from registry_exc
        raise OpsError("could not create isolated restore database") from exc


def provision_isolated_target(
    *,
    target_database_url: str,
    target_media_root: str | os.PathLike[str],
    restore_parent: str | os.PathLike[str],
    production_database_urls: Iterable[str],
    production_media_roots: Iterable[str | os.PathLike[str]] = (),
    backup_root: str | os.PathLike[str] | None = None,
) -> str:
    """Validate media safety first, then create and mark the owned database."""
    parent, target = _validate_isolated_media_target(
        target_media_root,
        restore_parent,
        backup_root=backup_root,
        production_media_roots=production_media_roots,
    )
    recovered_marker = recover_provisioning(
        target_database_url=target_database_url,
        restore_parent=parent,
        target_media_root=target,
    )
    marker = recovered_marker
    if marker is None:
        if target.exists() or os.path.lexists(target):
            raise OpsError(
                "restore MEDIA_ROOT already exists without recovery ownership; "
                "manual cleanup is required"
            )
        marker = create_isolated_database(
            target_database_url,
            production_database_urls,
            restore_parent=parent,
            target_media_root=target,
        )
    try:
        if not target.exists() and not os.path.lexists(target):
            target.mkdir(mode=0o700)
            os.chmod(target, 0o700)
    except Exception as exc:
        try:
            _drop_owned_database(
                target_database_url=target_database_url,
                ownership_marker=marker,
                restore_parent=parent,
                target_media_root=target,
                production_database_urls=production_database_urls,
                production_media_roots=production_media_roots,
                backup_root=backup_root,
                remove_media=False,
            )
        except OpsError as rollback_exc:
            try:
                _mark_restore_failed(parent, target, poisoned=True)
            except OpsError:
                pass
            raise OpsError("restore target provisioning failed; target is poisoned") from rollback_exc
        raise OpsError("could not provision isolated restore target") from exc
    return marker


def assert_isolated_targets(
    *,
    target_database_url: str,
    target_media_root: str | os.PathLike[str],
    backup_root: str | os.PathLike[str],
    generation_path: str | os.PathLike[str],
    production_database_urls: Iterable[str] = (),
    production_media_roots: Iterable[str | os.PathLike[str]] = (),
    source_database_url: str | None = None,
    _identity_resolver: Callable[[str], tuple[object, ...]] | None = None,
) -> Path:
    """Reject production or backup targets before any restore command runs."""
    database_candidates = [
        value
        for value in (
            source_database_url,
            os.environ.get("DATABASE_URL"),
            os.environ.get("PRODUCTION_DATABASE_URL"),
            *production_database_urls,
        )
        if isinstance(value, str) and value.strip()
    ]
    if not database_candidates:
        raise OpsError("a production database identifier is required")

    target = _path(target_media_root, "restore MEDIA_ROOT")
    _assert_no_symlink_components(target)
    backup = _path(backup_root, "BACKUP_ROOT").absolute()
    generation = _path(generation_path, "backup generation").absolute()
    _assert_no_symlink_components(backup, allow_missing_leaf=False)
    _assert_no_symlink_components(generation)
    if _path_overlap(target, backup) or _path_overlap(target, generation):
        raise OpsError("restore MEDIA_ROOT must be outside backup storage")
    production_paths = [
        value
        for value in (
            os.environ.get("MEDIA_ROOT"),
            os.environ.get("PRODUCTION_MEDIA_ROOT"),
            *production_media_roots,
        )
        if isinstance(value, (str, os.PathLike)) and os.fspath(value)
    ]
    if not production_paths:
        raise OpsError("a production MEDIA_ROOT identifier is required")
    for candidate in production_paths:
        production = _path(candidate, "production MEDIA_ROOT")
        _assert_no_symlink_components(production, allow_missing_leaf=False)
        if _path_overlap(target, production):
            raise OpsError("restore MEDIA_ROOT is a production media destination")
    if target.exists():
        _private_directory(target, create=False)
        try:
            if any(target.iterdir()):
                raise OpsError("restore MEDIA_ROOT must be empty and isolated")
        except OSError as exc:
            raise OpsError("restore MEDIA_ROOT is not usable") from exc
    else:
        _private_directory(target.parent, create=False)

    resolver = _identity_resolver or _connection_database_identity
    target_identity = resolver(target_database_url)
    for candidate in database_candidates:
        if _same_database(target_identity, resolver(candidate)):
            raise OpsError("restore target database is a production database")
    return target


def _copy_restore_file(source: Path, destination: Path) -> None:
    _private_file(source, label="backup media file")
    _assert_no_symlink_components(destination.parent)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink_components(destination.parent, allow_missing_leaf=False)
    os.chmod(destination.parent, 0o700)
    try:
        with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
            while chunk := source_stream.read(1024 * 1024):
                destination_stream.write(chunk)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
        os.chmod(destination, 0o600)
    except (FileExistsError, OSError) as exc:
        raise OpsError("could not restore media file") from exc


def _validate_tar_members(archive: Path) -> None:
    """Reject links, devices, and traversal before any archive extraction."""
    import tarfile

    _private_file(archive, label="media archive")
    try:
        with tarfile.open(archive, "r:*") as stream:
            for member in stream.getmembers():
                pure = PurePosixPath(member.name)
                if (
                    not member.name
                    or member.name.startswith("/")
                    or "\\" in member.name
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or member.issym()
                    or member.islnk()
                    or not (member.isdir() or member.isfile())
                ):
                    raise OpsError("media archive contains an unsafe member")
    except OpsError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise OpsError("media archive is invalid") from exc


def _make_immutable_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            _private_file(path, label="restore staging file")
            os.chmod(path, 0o400)
        for name in directories:
            path = current_path / name
            _private_directory(path, create=False)
            os.chmod(path, 0o500)
    os.chmod(root, 0o500)


def stage_verified_generation(
    generation: VerifiedGeneration,
    restore_parent: Path,
    *,
    pg_restore: str = "pg_restore",
    target: Path | None = None,
    run_id: str | None = None,
) -> tuple[VerifiedGeneration, Path]:
    """Copy a verified generation before restore, closing source TOCTOU."""
    parent = _private_directory(restore_parent, create=False)
    staging = parent / f".restore-source-{uuid.uuid4().hex}"
    _assert_no_symlink_components(staging)
    stage_run_id = run_id or uuid.uuid4().hex
    old_umask = os.umask(0o077)
    completed = False
    try:
        staging.mkdir(mode=0o700)
        _write_restore_stage_owner(
            staging,
            parent=parent,
            target=target,
            run_id=stage_run_id,
            kind="source",
        )
        staged_generation = staging / generation.name
        staged_generation.mkdir(mode=0o700)
        (staged_generation / "media").mkdir(mode=0o700)
        for directory_name in MEDIA_DIRS:
            (staged_generation / "media" / directory_name).mkdir(mode=0o700)
        for raw in generation.files:
            relative = _safe_manifest_path(raw["path"])
            source = generation.path / Path(*PurePosixPath(relative).parts)
            destination = staged_generation / Path(*PurePosixPath(relative).parts)
            _copy_restore_file(source, destination)
        _copy_restore_file(generation.path / MANIFEST_NAME, staged_generation / MANIFEST_NAME)
        _fsync_directory(staged_generation)
        copied = verify_generation(staged_generation)
        if copied.manifest != generation.manifest:
            raise OpsError("backup generation changed while being staged")
        dump = staged_generation / DATABASE_DUMP_NAME
        try:
            with dump.open("rb") as stream:
                if stream.read(len(POSTGRES_DUMP_MAGIC)) != POSTGRES_DUMP_MAGIC:
                    raise OpsError("staged PostgreSQL dump is not a PGDMP file")
        except OpsError:
            raise
        except OSError as exc:
            raise OpsError("could not inspect staged PostgreSQL dump") from exc
        _run_native([pg_restore, "--list", str(dump)], label="staged pg_restore validation")
        _make_immutable_tree(staged_generation)
        _remove_restore_stage_owner(staging)
        os.chmod(staging, 0o500)
        completed = True
        return copied, staging
    except OpsError:
        raise
    except OSError as exc:
        raise OpsError("could not stage backup generation") from exc
    finally:
        os.umask(old_umask)
        if not completed and (staging.exists() or os.path.lexists(staging)):
            _remove_private_path_strict(staging)


def restore_media(
    generation: VerifiedGeneration,
    target: Path,
    *,
    run_id: str | None = None,
    restore_parent: Path | None = None,
) -> None:
    """Copy media through a private staging tree and atomically publish it."""
    _assert_no_symlink_components(target)
    if target.exists():
        _private_directory(target, create=False)
        try:
            if any(target.iterdir()):
                raise OpsError("restore MEDIA_ROOT must be empty")
        except OSError as exc:
            raise OpsError("restore MEDIA_ROOT is not usable") from exc
    parent = _private_directory(restore_parent or target.parent, create=False)
    try:
        target.relative_to(parent)
    except ValueError as exc:
        raise OpsError("restore target must be inside the private restore parent") from exc
    staging = parent / f".restore-{uuid.uuid4().hex}"
    stage_run_id = run_id or uuid.uuid4().hex
    old_umask = os.umask(0o077)
    try:
        staging.mkdir(mode=0o700)
        _write_restore_stage_owner(
            staging,
            parent=parent,
            target=target,
            run_id=stage_run_id,
            kind="media",
        )
        for directory_name in MEDIA_DIRS:
            (staging / directory_name).mkdir(mode=0o700)
        for entry in generation.files:
            relative = str(entry["path"])
            if not relative.startswith("media/"):
                continue
            source = generation.path / Path(*PurePosixPath(relative).parts)
            destination = staging / Path(*PurePosixPath(relative).parts[1:])
            _copy_restore_file(source, destination)
        for current, directories, files in os.walk(staging, topdown=True, followlinks=False):
            _private_directory(Path(current), create=False)
            for name in directories:
                _private_directory(Path(current) / name, create=False)
            for name in files:
                _private_file(Path(current) / name, label="restored media file")
        _remove_restore_stage_owner(staging)
        _fsync_directory(staging)
        if target.exists():
            target.rmdir()
        os.replace(staging, target)
        os.chmod(target, 0o700)
        _fsync_directory(parent)
    except OpsError:
        raise
    except OSError as exc:
        raise OpsError("isolated media restore failed") from exc
    finally:
        os.umask(old_umask)
        if staging.exists() or os.path.lexists(staging):
            _remove_private_path_strict(staging)


def verify_restored_media(generation: VerifiedGeneration, target: Path) -> None:
    """Recheck copied media against the generation manifest before DB use."""
    for entry in generation.files:
        relative = str(entry["path"])
        if not relative.startswith("media/"):
            continue
        destination = target / Path(*PurePosixPath(relative).parts[1:])
        size, digest = _sha256_file(destination)
        if size != entry["bytes"] or digest != entry["sha256"]:
            raise OpsError("restored media checksum verification failed")


def restore_database(
    generation: VerifiedGeneration,
    *,
    target_database_url: str,
    pg_restore: str = "pg_restore",
) -> None:
    dump = generation.path / DATABASE_DUMP_NAME
    _private_file(dump, label="PostgreSQL dump")
    with _postgres_environment(target_database_url) as database_environment:
        command = [
            pg_restore,
            "--exit-on-error",
            "--single-transaction",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            database_environment["PGDATABASE"],
            str(dump),
        ]
        _run_native(command, label="pg_restore", env=database_environment)


def migration_check(
    *,
    target_database_url: str,
    target_media_root: Path,
    repo_root: Path,
    alembic_ini: Path | None = None,
) -> None:
    config = alembic_ini or repo_root / "alembic.ini"
    if not config.is_file() or config.is_symlink():
        raise OpsError("Alembic configuration is unavailable")
    with _postgres_environment(target_database_url, base=os.environ) as environment:
        environment.update(
            {
                "DATABASE_URL": _native_postgres_url(target_database_url),
                "MEDIA_ROOT": str(target_media_root),
                "APP_ENV": "test",
            }
        )
        for action in ("upgrade", "current"):
            command = [sys.executable, "-m", "alembic", "-c", str(config), action, "head"] if action == "upgrade" else [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(config),
                action,
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=repo_root,
                    env=environment,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except (FileNotFoundError, OSError) as exc:
                raise OpsError("Alembic migration check could not be started") from exc
            if completed.returncode != 0:
                raise OpsError("Alembic migration check failed")
            if action == "current" and "head" not in (completed.stdout + completed.stderr).casefold():
                raise OpsError("restored database is not at the current migration head")


def _database_media_check(application, media_root: Path) -> None:
    from sqlalchemy import select

    from app.db import db
    from app.models import Capture, Export
    from app.storage import ManagedStorage, StorageError

    storage = ManagedStorage(media_root)
    with application.app_context():
        for row in db.session.scalars(select(Capture)):
            try:
                path = storage.resolve(row.storage_key)
                size, digest = _sha256_file(path)
            except (StorageError, OpsError) as exc:
                raise OpsError("restored Capture media is missing or unsafe") from exc
            if size != row.byte_count or digest != row.sha256:
                raise OpsError("restored Capture media checksum does not match the database")
        for row in db.session.scalars(select(Export)):
            try:
                path = storage.resolve(row.storage_key)
                size, digest = _sha256_file(path)
            except (StorageError, OpsError) as exc:
                raise OpsError("restored Export media is missing or unsafe") from exc
            if size != row.byte_count or digest != row.sha256:
                raise OpsError("restored Export media checksum does not match the database")


def _csrf_token(response) -> str:
    match = _CSRF_RE.search(response.get_data(as_text=True))
    if match is None:
        raise OpsError("restore smoke login form has no CSRF token")
    return match.group(1)


def smoke_check(
    *,
    target_database_url: str,
    target_media_root: Path,
    username: str,
    password: str,
) -> None:
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise OpsError("restore smoke credentials are required")
    try:
        from flask import Flask
        from sqlalchemy import select
        from app import create_app
        from app.db import db
        from app.models import ComparisonSet, User
    except (ImportError, RuntimeError) as exc:
        raise OpsError("application dependencies are unavailable for restore smoke") from exc
    with scoped_postgres_environment(target_database_url):
        application: Flask = create_app(
            {
                "TESTING": True,
                "APP_ENV": "test",
                "DATABASE_URL": target_database_url,
                "MEDIA_ROOT": str(target_media_root),
                "SECRET_KEY": secrets.token_urlsafe(32),
                "SESSION_COOKIE_SECURE": False,
                "BOARD_RENDER_DPI": 20,
            }
        )
        client = application.test_client()
        login_page = client.get("/login")
        if login_page.status_code != 200:
            raise OpsError("restore smoke login page failed")
        token = _csrf_token(login_page)
        login_response = client.post(
            "/login",
            data={"username": username, "password": password, "csrf_token": token},
            follow_redirects=False,
        )
        if login_response.status_code != 302:
            raise OpsError("restore smoke login failed")
        with application.app_context():
            user = db.session.scalar(select(User).where(User.username == username.casefold()))
            if user is None or not user.active or not user.is_editor:
                raise OpsError("restore smoke user must be an active Editor or Admin")
            comparison_set = db.session.scalar(
                select(ComparisonSet)
                .where(ComparisonSet.archived_at.is_(None))
                .order_by(ComparisonSet.id)
            )
            if comparison_set is None:
                raise OpsError("restored database has no active Comparison Set for smoke")
            set_id = comparison_set.id
            version = comparison_set.version
        patients = client.get("/patients")
        if patients.status_code != 200:
            raise OpsError("restore smoke read failed")
        detail = client.get(f"/comparison-sets/{set_id}")
        if detail.status_code != 200:
            raise OpsError("restore smoke Comparison Set read failed")
        preview = client.get(f"/comparison-sets/{set_id}/preview?version={version}")
        if preview.status_code != 200:
            raise OpsError("restore smoke preview failed")
        export_token = _csrf_token(detail)
        exported = client.post(
            f"/comparison-sets/{set_id}/export",
            data={"format": "png", "version": str(version), "csrf_token": export_token},
        )
        if exported.status_code != 200 or not exported.data.startswith(b"\x89PNG"):
            raise OpsError("restore smoke export failed")


def assert_active_login(application) -> None:
    """Require an active account from the restored backup before provisioning smoke credentials."""
    from sqlalchemy import select

    from app.db import db
    from app.models import User

    with application.app_context():
        if db.session.scalar(select(User.id).where(User.active.is_(True)).limit(1)) is None:
            raise OpsError("restored database has no active login")


def ensure_smoke_account(application, username: str, password: str) -> bool:
    """Create a disposable Editor in the restored DB only when it is absent."""
    from sqlalchemy import select

    from app.auth import AdminError, create_user
    from app.db import db
    from app.models import User

    with application.app_context():
        existing = db.session.scalar(select(User).where(User.username == username.casefold()))
        if existing is not None:
            if existing.active and existing.is_editor:
                return False
            raise OpsError("restore smoke account exists but is not an active Editor or Admin")
        try:
            create_user(
                actor=None,
                username=username,
                display_name=_SMOKE_ACCOUNT_DISPLAY_NAME,
                password=password,
                role="editor",
                active=True,
                bootstrap=True,
            )
        except (AdminError, ValueError) as exc:
            raise OpsError("could not create restore smoke account") from exc
    return True


def remove_smoke_account(application, username: str) -> None:
    """Disable the smoke account without breaking its audit-event foreign keys."""
    from sqlalchemy import select

    from app.db import db
    from app.models import User

    with application.app_context():
        user = db.session.scalar(select(User).where(User.username == username.casefold()))
        if user is None:
            raise OpsError("temporary restore smoke account is missing during cleanup")
        try:
            user.active = False
            user.session_version += 1
            user.set_password(secrets.token_urlsafe(48))
            if user.display_name == _SMOKE_ACCOUNT_DISPLAY_NAME:
                user.display_name = "Disabled restore smoke account"
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            raise OpsError("could not disable temporary restore smoke account") from exc


def _remove_private_path(path: Path) -> None:
    """Remove only the named restore artifact and report deletion failures."""
    _remove_private_path_strict(path)


def _remove_private_path_strict(path: Path) -> None:
    """Remove a restore artifact and report cleanup errors to the caller."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OpsError("could not inspect restore cleanup path") from exc
    try:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            path.unlink()
            return
        for current, directories, files in os.walk(path, topdown=False, followlinks=False):
            current_path = Path(current)
            for name in files:
                file_path = current_path / name
                if not file_path.is_symlink():
                    os.chmod(file_path, 0o600)
            for name in directories:
                directory = current_path / name
                if not directory.is_symlink():
                    os.chmod(directory, 0o700)
        os.chmod(path, 0o700)
        shutil.rmtree(path)
    except OSError as exc:
        raise OpsError("could not remove restore cleanup path") from exc


def _maintenance_database_url(value: str) -> str:
    parsed = urlsplit(_native_postgres_url(value))
    if not parsed.netloc:
        query = f"?{parsed.query}" if parsed.query else ""
        return f"postgresql:///postgres{query}"
    return urlunsplit(("postgresql", parsed.netloc, "/postgres", parsed.query, parsed.fragment))


def _drop_owned_database(
    *,
    target_database_url: str,
    ownership_marker: str,
    restore_parent: str | os.PathLike[str],
    target_media_root: str | os.PathLike[str],
    production_database_urls: Iterable[str],
    production_media_roots: Iterable[str | os.PathLike[str]] = (),
    backup_root: str | os.PathLike[str] | None = None,
    remove_media: bool = True,
) -> None:
    """Drop only a database carrying this run's ownership marker."""
    if _OWNER_MARKER_RE.fullmatch(ownership_marker) is None:
        raise OpsError("restore ownership marker is invalid")
    production_urls = tuple(
        value for value in production_database_urls if isinstance(value, str) and value.strip()
    )
    if not production_urls:
        raise OpsError("a production database identifier is required before cleanup")
    target = _path(target_media_root, "restore MEDIA_ROOT")
    parent = _private_directory(_path(restore_parent, "restore parent"), create=False)
    if remove_media:
        _assert_no_symlink_components(target)
        if target == parent or not _path_overlap(target, parent):
            raise OpsError("restore target must be inside the private restore parent")
        if backup_root is not None and _path_overlap(target, _path(backup_root, "BACKUP_ROOT")):
            raise OpsError("restore MEDIA_ROOT must be outside backup storage")
        for value in production_media_roots:
            production = _path(value, "production MEDIA_ROOT")
            _assert_no_symlink_components(production, allow_missing_leaf=False)
            if _path_overlap(target, production):
                raise OpsError("restore MEDIA_ROOT is a production media destination")
    else:
        try:
            target.relative_to(parent)
        except ValueError as exc:
            raise OpsError("restore target must be inside the private restore parent") from exc
        if target == parent:
            raise OpsError("restore target must be inside the private restore parent")
    if _read_owner_marker(parent, target) != ownership_marker:
        raise OpsError("restore ownership marker does not match this run")

    target_server = _connection_server_identity(target_database_url)
    target_identity = (
        _connection_database_identity(target_database_url)
        if target_server[:1] == ("cluster",)
        else None
    )
    target_name = _database_name(target_database_url)
    for production_url in production_urls:
        production_identity = _connection_database_identity(production_url)
        if target_identity is not None and _same_database(target_identity, production_identity):
            raise OpsError("refusing to clean a production database")
    try:
        import psycopg
    except ImportError as exc:
        raise OpsError("psycopg is required to clean the restore database") from exc
    try:
        with scoped_postgres_environment(target_database_url), _psycopg_connect(_maintenance_database_url(target_database_url), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
                    (target_name,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise OpsError("owned restore database is missing")
                if row[0] != ownership_marker:
                    raise OpsError("restore database ownership marker does not match")
                cursor.execute(f'DROP DATABASE "{target_name}" WITH (FORCE)')
    except OpsError:
        raise
    except Exception as exc:
        raise OpsError("could not drop owned isolated restore database") from exc
    if remove_media:
        _remove_private_path_strict(target)
    _remove_private_path_strict(_owner_marker_path(parent, target))


def _assert_owned_database(target_database_url: str, ownership_marker: str) -> None:
    """Verify the database comment before any restore command can mutate it."""
    if _OWNER_MARKER_RE.fullmatch(ownership_marker) is None:
        raise OpsError("restore ownership marker is invalid")
    target_name = _database_name(target_database_url)
    try:
        import psycopg
    except ImportError as exc:
        raise OpsError("psycopg is required to verify restore ownership") from exc
    try:
        with scoped_postgres_environment(target_database_url), _psycopg_connect(_maintenance_database_url(target_database_url), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
                    (target_name,),
                )
                row = cursor.fetchone()
    except Exception as exc:
        raise OpsError("could not verify restore database ownership") from exc
    if row is None or row[0] != ownership_marker:
        raise OpsError("restore target database is not owned by this run")


def _set_owned_database_comment(target_database_url: str, ownership_marker: str) -> None:
    """Restore the ownership comment only if no other owner claimed the DB."""
    if _OWNER_MARKER_RE.fullmatch(ownership_marker) is None:
        raise OpsError("restore ownership marker is invalid")
    target_name = _database_name(target_database_url)
    try:
        import psycopg
        with scoped_postgres_environment(target_database_url), _psycopg_connect(_maintenance_database_url(target_database_url), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
                    (target_name,),
                )
                row = cursor.fetchone()
                if row is None or row[0] not in {None, ownership_marker}:
                    raise OpsError("restore database ownership marker changed during restore")
                cursor.execute(f"COMMENT ON DATABASE \"{target_name}\" IS '{ownership_marker}'")
    except OpsError:
        raise
    except Exception as exc:
        raise OpsError("could not preserve restore database ownership marker") from exc


def _cleanup_restore_database(target_database_url: str) -> None:
    """Legacy test seam; production cleanup requires an ownership marker."""
    raise OpsError("restore database cleanup requires an ownership marker")


def _mark_restore_failed(parent: Path, target: Path, *, poisoned: bool = False) -> None:
    marker = parent / f".{target.name}.restore-failed"
    _assert_no_symlink_components(marker)
    message = "restore target is failed and disposable; destroy its database and retry"
    if poisoned:
        message = "restore target is POISONED; cleanup failed; destroy its database before retry"
    try:
        with marker.open("w", encoding="ascii") as stream:
            stream.write(message + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(marker, 0o600)
        _fsync_directory(parent)
    except OSError as exc:
        raise OpsError("could not mark failed restore target") from exc


def _read_secret_file(path: str | os.PathLike[str]) -> str:
    secret_path = _path(path, "restore smoke password file")
    _private_file(secret_path, label="restore smoke password file")
    try:
        value = secret_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise OpsError("could not read restore smoke password file") from exc
    return value.rstrip("\r\n")


def _validate_isolated_media_target(
    target_media_root: str | os.PathLike[str],
    restore_parent: str | os.PathLike[str],
    *,
    backup_root: str | os.PathLike[str] | None = None,
    production_media_roots: Iterable[str | os.PathLike[str]] = (),
) -> tuple[Path, Path]:
    """Validate every media boundary without creating the target."""
    target = _path(target_media_root, "restore MEDIA_ROOT")
    parent = _private_directory(_path(restore_parent, "restore parent"), create=False)
    _assert_no_symlink_components(target)
    try:
        target.relative_to(parent)
    except ValueError as exc:
        raise OpsError("restore target must be inside the private restore parent") from exc
    if target == parent:
        raise OpsError("restore target must be inside the private restore parent")
    ancestor = target.parent
    while ancestor != parent:
        _private_directory(ancestor, create=False)
        ancestor = ancestor.parent
    if backup_root is not None:
        backup = _path(backup_root, "BACKUP_ROOT")
        _assert_no_symlink_components(backup, allow_missing_leaf=False)
        if _path_overlap(target, backup):
            raise OpsError("restore MEDIA_ROOT must be outside backup storage")
    for value in production_media_roots:
        production = _path(value, "production MEDIA_ROOT")
        _assert_no_symlink_components(production, allow_missing_leaf=False)
        if _path_overlap(target, production):
            raise OpsError("restore MEDIA_ROOT is a production media destination")
    if target.exists() or os.path.lexists(target):
        _private_directory(target, create=False)
    return parent, target


def prepare_isolated_media_target(
    target_media_root: str | os.PathLike[str],
    restore_parent: str | os.PathLike[str],
    *,
    backup_root: str | os.PathLike[str] | None = None,
    production_media_roots: Iterable[str | os.PathLike[str]] = (),
) -> Path:
    """Create the empty `0700` media target inside a private restore parent."""
    _parent, target = _validate_isolated_media_target(
        target_media_root,
        restore_parent,
        backup_root=backup_root,
        production_media_roots=production_media_roots,
    )
    try:
        target.mkdir(mode=0o700)
        os.chmod(target, 0o700)
    except OSError as exc:
        raise OpsError("could not create isolated restore MEDIA_ROOT") from exc
    return target


def cleanup_isolated_target(
    *,
    target_database_url: str,
    target_media_root: str | os.PathLike[str],
    restore_parent: str | os.PathLike[str],
    production_database_urls: Iterable[str],
    production_media_roots: Iterable[str | os.PathLike[str]] = (),
    backup_root: str | os.PathLike[str] | None = None,
    ownership_marker: str | None = None,
) -> None:
    """Drop only this run's owned database and remove its restore media."""
    target = _path(target_media_root, "restore MEDIA_ROOT")
    parent = _private_directory(_path(restore_parent, "restore parent"), create=False)
    marker = ownership_marker or _read_owner_marker(parent, target)
    try:
        _drop_owned_database(
            target_database_url=target_database_url,
            ownership_marker=marker,
            restore_parent=parent,
            target_media_root=target,
            production_database_urls=production_database_urls,
            production_media_roots=production_media_roots,
            backup_root=backup_root,
        )
        cleanup_stale_restore_staging(
            restore_parent=parent,
            target_media_root=target,
            run_id=marker,
        )
    except OpsError as exc:
        try:
            _mark_restore_failed(parent, target, poisoned=True)
        except OpsError as marker_exc:
            raise OpsError("isolated target cleanup failed; target is poisoned") from marker_exc
        raise OpsError("isolated target cleanup failed; target is poisoned") from exc
    try:
        _remove_private_path_strict(parent / f".{target.name}.restore-failed")
    except OpsError as exc:
        try:
            _mark_restore_failed(parent, target, poisoned=True)
        except OpsError as marker_exc:
            raise OpsError("isolated target cleanup failed; target is poisoned") from marker_exc
        raise OpsError("isolated target cleanup failed; target is poisoned") from exc


def run_restore_check(
    *,
    backup_root: str | os.PathLike[str],
    target_database_url: str,
    target_media_root: str | os.PathLike[str],
    generation: str | None = None,
    production_database_urls: Iterable[str] = (),
    production_media_roots: Iterable[str | os.PathLike[str]] = (),
    source_database_url: str | None = None,
    pg_dump: str = "pg_dump",
    psql: str = "psql",
    pg_restore: str = "pg_restore",
    repo_root: str | os.PathLike[str] | None = None,
    alembic_ini: str | os.PathLike[str] | None = None,
    restore_parent: str | os.PathLike[str] | None = None,
    ownership_marker: str | None = None,
    smoke_username: str | None = None,
    smoke_password: str | None = None,
    create_smoke_account: bool = False,
) -> RestoreResult:
    if not isinstance(smoke_username, str) or not smoke_username:
        raise OpsError("restore smoke credentials are required")
    if not isinstance(smoke_password, str) or not smoke_password:
        raise OpsError("restore smoke credentials are required")
    selected = select_generation(backup_root, generation)
    target = assert_isolated_targets(
        target_database_url=target_database_url,
        target_media_root=target_media_root,
        backup_root=backup_root,
        generation_path=selected.path,
        production_database_urls=production_database_urls,
        production_media_roots=production_media_roots,
        source_database_url=source_database_url,
    )
    postgres_restore_preflight(
        selected,
        target_database_url,
        pg_dump=pg_dump,
        pg_restore=pg_restore,
        psql=psql,
        source_database_url=source_database_url,
    )
    parent = _private_directory(
        _path(restore_parent, "restore parent") if restore_parent is not None else target.parent,
        create=False,
    )
    if target == parent or not _path_overlap(target, parent):
        raise OpsError("restore target must be inside the private restore parent")
    postgres_metadata = selected.manifest.get("postgresql")
    test_only_target = isinstance(postgres_metadata, dict) and postgres_metadata.get("test_only") is True
    if test_only_target:
        if ownership_marker is not None:
            raise OpsError("test-only restore targets cannot carry a production ownership marker")
    else:
        if not isinstance(ownership_marker, str):
            raise OpsError("restore target must be provisioned by this run")
        if _read_owner_marker(parent, target) != ownership_marker:
            raise OpsError("restore ownership marker does not match this run")
        _assert_owned_database(target_database_url, ownership_marker)
    stage_run_id = (
        _restore_run_id(ownership_marker)
        if isinstance(ownership_marker, str)
        else uuid.uuid4().hex
    )
    if isinstance(ownership_marker, str):
        cleanup_stale_restore_staging(
            restore_parent=parent,
            target_media_root=target,
            run_id=stage_run_id,
        )
    source_staging: Path | None = None
    completed = False
    smoke_account_created = False
    try:
        staged, source_staging = stage_verified_generation(
            selected,
            parent,
            pg_restore=pg_restore,
            target=target,
            run_id=stage_run_id,
        )
        if test_only_target:
            restore_media(staged, target)
        else:
            restore_media(staged, target, run_id=stage_run_id, restore_parent=parent)
        verify_restored_media(staged, target)
        restore_database(staged, target_database_url=target_database_url, pg_restore=pg_restore)
        if not test_only_target:
            _set_owned_database_comment(target_database_url, ownership_marker or "")
        root = _path(repo_root, "repository root") if repo_root is not None else Path(__file__).resolve().parents[1]
        config = _path(alembic_ini, "alembic configuration") if alembic_ini is not None else None
        migration_check(
            target_database_url=target_database_url,
            target_media_root=target,
            repo_root=root,
            alembic_ini=config,
        )
        try:
            from app import create_app
        except ImportError as exc:
            raise OpsError("application dependencies are unavailable for restore check") from exc
        with scoped_postgres_environment(target_database_url):
            application = create_app(
                {
                    "TESTING": True,
                    "APP_ENV": "test",
                    "DATABASE_URL": target_database_url,
                    "MEDIA_ROOT": str(target),
                    "SECRET_KEY": secrets.token_urlsafe(32),
                    "SESSION_COOKIE_SECURE": False,
                }
            )
            if create_smoke_account:
                assert_active_login(application)
                smoke_account_created = ensure_smoke_account(application, smoke_username, smoke_password)
            _database_media_check(application, target)
            smoke_check(
                target_database_url=target_database_url,
                target_media_root=target,
                username=smoke_username,
                password=smoke_password,
            )
        completed = True
        return RestoreResult(
            generation=selected.name,
            media_files=sum(1 for item in staged.files if str(item["path"]).startswith("media/")),
            migration_checked=True,
            database_media_checked=True,
            smoke_checked=True,
        )
    except OpsError:
        raise
    except Exception as exc:
        raise OpsError("isolated restore check failed") from exc
    finally:
        cleanup_errors: list[BaseException] = []
        if smoke_account_created:
            try:
                with scoped_postgres_environment(target_database_url):
                    remove_smoke_account(application, smoke_username)
            except OpsError as exc:
                cleanup_errors.append(exc)
        if source_staging is not None:
            try:
                _remove_private_path_strict(source_staging)
            except OpsError as exc:
                cleanup_errors.append(exc)
        if not completed:
            try:
                _remove_private_path_strict(target)
            except OpsError as exc:
                cleanup_errors.append(exc)
            if test_only_target:
                try:
                    _cleanup_restore_database(target_database_url)
                except OpsError as exc:
                    cleanup_errors.append(exc)
            try:
                _mark_restore_failed(parent, target, poisoned=bool(cleanup_errors))
            except OpsError as exc:
                cleanup_errors.append(exc)
            if cleanup_errors:
                raise OpsError("restore failed and cleanup failed; target is poisoned") from cleanup_errors[0]
        elif cleanup_errors:
            try:
                _mark_restore_failed(parent, target, poisoned=True)
            except OpsError as exc:
                cleanup_errors.append(exc)
            raise OpsError("restore passed checks but cleanup failed; target is poisoned") from cleanup_errors[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-root", default=os.environ.get("BACKUP_ROOT"), required=False)
    parser.add_argument("--generation", default=os.environ.get("BACKUP_GENERATION"))
    parser.add_argument("--target-database-url", default=os.environ.get("RESTORE_CHECK_DATABASE_URL"))
    parser.add_argument("--target-media-root", default=os.environ.get("RESTORE_CHECK_MEDIA_ROOT"))
    parser.add_argument(
        "--production-database-url",
        default=os.environ.get("PRODUCTION_DATABASE_URL", os.environ.get("DATABASE_URL")),
    )
    parser.add_argument(
        "--production-media-root",
        default=os.environ.get("PRODUCTION_MEDIA_ROOT", os.environ.get("MEDIA_ROOT")),
    )
    parser.add_argument("--source-database-url", default=os.environ.get("BACKUP_SOURCE_DATABASE_URL"))
    parser.add_argument("--pg-dump", default=os.environ.get("PG_DUMP", "pg_dump"))
    parser.add_argument("--pg-restore", default=os.environ.get("PG_RESTORE", "pg_restore"))
    parser.add_argument("--psql", default=os.environ.get("PSQL", "psql"))
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--alembic-ini", default=None)
    parser.add_argument("--restore-parent", default=os.environ.get("RESTORE_CHECK_PARENT"))
    parser.add_argument("--smoke-username", default=os.environ.get("RESTORE_SMOKE_USERNAME"))
    parser.add_argument("--smoke-password-file", default=os.environ.get("RESTORE_SMOKE_PASSWORD_FILE"))
    parser.add_argument("--smoke-password-stdin", action="store_true")
    parser.add_argument(
        "--create-smoke-account",
        action="store_true",
        help="create an Editor in the restored target when the named account is absent",
    )
    parser.add_argument(
        "--provision-target",
        action="store_true",
        help="create the empty 0700 media target and a new isolated PostgreSQL database first",
    )
    parser.add_argument(
        "--cleanup-target",
        action="store_true",
        help="drop the guarded isolated database and remove its media target",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="required acknowledgement that the database/media targets are disposable",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.isolated:
            raise OpsError("restore-check requires --isolated")
        production_database_urls = (
            (args.production_database_url,) if args.production_database_url else ()
        )
        if args.cleanup_target:
            if not args.target_database_url or not args.target_media_root or not args.restore_parent:
                raise OpsError("target database, media root, and restore parent are required for cleanup")
            cleanup_isolated_target(
                target_database_url=args.target_database_url,
                target_media_root=args.target_media_root,
                restore_parent=args.restore_parent,
                production_database_urls=production_database_urls,
                production_media_roots=(args.production_media_root,) if args.production_media_root else (),
                backup_root=args.backup_root,
            )
            print("restore target cleaned")
            return 0
        if not args.provision_target:
            raise OpsError("restore-check requires --provision-target; existing targets are never restored")
        if args.smoke_password_file:
            smoke_password = _read_secret_file(args.smoke_password_file)
        elif args.smoke_password_stdin:
            smoke_password = sys.stdin.readline().rstrip("\r\n")
        else:
            raise OpsError("restore smoke password file or stdin is required")
        if not args.target_database_url or not args.target_media_root or not args.restore_parent:
            raise OpsError("target database, media root, and restore parent are required to provision a restore target")
        ownership_marker = provision_isolated_target(
            target_database_url=args.target_database_url,
            target_media_root=args.target_media_root,
            restore_parent=args.restore_parent,
            production_database_urls=(
                *production_database_urls,
                *((args.source_database_url,) if args.source_database_url else ()),
            ),
            production_media_roots=(args.production_media_root,) if args.production_media_root else (),
            backup_root=args.backup_root,
        )
        result = run_restore_check(
            backup_root=args.backup_root,
            target_database_url=args.target_database_url,
            target_media_root=args.target_media_root,
            generation=args.generation,
            production_database_urls=(args.production_database_url,) if args.production_database_url else (),
            production_media_roots=(args.production_media_root,) if args.production_media_root else (),
            source_database_url=args.source_database_url,
            pg_dump=args.pg_dump,
            psql=args.psql,
            pg_restore=args.pg_restore,
            repo_root=args.repo_root,
            alembic_ini=args.alembic_ini,
            restore_parent=args.restore_parent,
            ownership_marker=ownership_marker,
            smoke_username=args.smoke_username,
            smoke_password=smoke_password,
            create_smoke_account=args.create_smoke_account,
        )
    except OpsError as exc:
        print(f"restore-check failed: {exc}", file=sys.stderr)
        return 1
    print(f"restore-check passed: generation={result.generation} media_files={result.media_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
