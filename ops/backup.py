#!/usr/bin/env python3
"""Create an atomic PostgreSQL/media backup generation.

This module deliberately has no Flask imports.  It is run by a scheduled
service while the application is stopped by ``ops/backup.sh``.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Iterator
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
import uuid


GENERATION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$")
MEDIA_DIRS = ("originals", "previews", "derivatives", "quarantine")
MANIFEST_NAME = "manifest.json"
DATABASE_DUMP_NAME = "database.dump"
MANIFEST_FORMAT_VERSION = 1
POSTGRES_DUMP_MAGIC = b"PGDMP"
_PG_ENV_QUERY_KEYS = {
    "application_name",
    "channel_binding",
    "connect_timeout",
    "gssencmode",
    "hostaddr",
    "keepalives",
    "keepalives_count",
    "keepalives_idle",
    "keepalives_interval",
    "sslcert",
    "sslkey",
    "sslmode",
    "sslrootcert",
}


class OpsError(RuntimeError):
    """Raised when an operational safety or backup invariant fails."""


@dataclass(frozen=True)
class BackupResult:
    generation: str
    path: Path
    manifest: dict[str, object]


def _path(value: str | os.PathLike[str], label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise OpsError(f"{label} is required")
    raw = os.fspath(value)
    if not raw:
        raise OpsError(f"{label} is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if ".." in path.parts:
        raise OpsError(f"{label} must not contain path traversal")
    return path


def _assert_no_symlink_components(path: Path, *, allow_missing_leaf: bool = True) -> None:
    """Reject symlinks in every existing component of a managed path."""
    path = path.absolute()
    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_leaf and index == len(parts) - 1:
                return
            raise OpsError(f"managed path is missing: {current.name}")
        except OSError as exc:
            raise OpsError("could not inspect managed path") from exc
        if stat.S_ISLNK(info.st_mode):
            raise OpsError("symlinks are not allowed in operational paths")


def _assert_no_posix_acl(path: Path) -> None:
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except (AttributeError, OSError) as exc:
        if isinstance(exc, AttributeError):
            return
        raise OpsError("could not inspect operational ACLs") from exc
    if any(name in {"system.posix_acl_access", "system.posix_acl_default"} for name in names):
        raise OpsError("operational paths must not have POSIX ACLs")


def _private_directory(path: Path, *, create: bool, media: bool = False) -> Path:
    path = _path(path, "directory")
    _assert_no_symlink_components(path)
    try:
        if path.exists():
            if not path.is_dir():
                raise OpsError("managed path must be a directory")
            mode = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
            if media:
                if mode & 0o007 or mode & 0o020 or mode & 0o070 != 0o050:
                    raise OpsError("managed media directories must be group-readable and not writable")
            elif mode & 0o077:
                raise OpsError("operational directories must not be group/world accessible")
        elif create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        else:
            raise OpsError("managed directory is missing")
        _assert_no_posix_acl(path)
    except OpsError:
        raise
    except OSError as exc:
        raise OpsError("managed directory is not usable") from exc
    return path


def _private_file(path: Path, *, label: str = "file", media: bool = False) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise OpsError(f"{label} is not usable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OpsError(f"{label} must be a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if media:
        if mode & 0o007 or mode & 0o020 or mode & 0o040 == 0:
            raise OpsError(f"{label} must be group-readable and not writable")
    elif mode & 0o077:
        raise OpsError(f"{label} must not be group/world accessible")
    _assert_no_posix_acl(path)


def _ensure_private_output_directory(path: Path) -> None:
    _private_directory(path, create=True)
    os.chmod(path, 0o700)


def _path_overlap(first: Path, second: Path) -> bool:
    try:
        first_real = first.resolve(strict=False)
        second_real = second.resolve(strict=False)
        common = Path(os.path.commonpath((str(first_real), str(second_real))))
    except ValueError:
        return False
    except OSError as exc:
        raise OpsError("could not resolve operational paths") from exc
    if common == first_real or common == second_real:
        return True
    try:
        if first.exists() and second.exists():
            first_stat = os.stat(first, follow_symlinks=False)
            second_stat = os.stat(second, follow_symlinks=False)
            if (first_stat.st_dev, first_stat.st_ino) == (second_stat.st_dev, second_stat.st_ino):
                return True
    except OSError as exc:
        raise OpsError("could not compare operational paths") from exc
    return False


def _backup_root(path: Path) -> Path:
    root = _private_directory(path, create=False)
    try:
        info = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise OpsError("BACKUP_ROOT is not usable") from exc
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise OpsError("BACKUP_ROOT must be mode 0700")
    if info.st_uid != os.geteuid():
        raise OpsError("BACKUP_ROOT must be owned by the backup service user")
    _assert_no_posix_acl(root)
    return root


def _run_output(command: list[str], *, label: str) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        raise OpsError(f"{label} is unavailable") from exc
    if completed.returncode != 0:
        raise OpsError(f"{label} failed with exit status {completed.returncode}")
    return completed.stdout.strip()


def _mount_info(path: Path, *, require_non_root_mount: bool = False) -> tuple[str, Path, str]:
    output = _run_output(
        [
            "findmnt",
            "--noheadings",
            "--output",
            "SOURCE,TARGET,FSTYPE",
            "--target",
            str(path),
        ],
        label="findmnt",
    )
    fields = output.split(None, 2)
    if len(fields) != 3 or not all(fields):
        raise OpsError("findmnt did not identify a real backup filesystem")
    source, target, fstype = fields
    mount_target = Path(target).resolve(strict=False)
    if require_non_root_mount and mount_target == Path("/"):
        raise OpsError("BACKUP_ROOT is not on a mounted backup filesystem")
    try:
        path.resolve(strict=False).relative_to(mount_target)
    except ValueError as exc:
        raise OpsError("BACKUP_ROOT is outside its reported mount") from exc
    return source, mount_target, fstype


def _top_level_devices(source: str) -> tuple[str, ...]:
    if not source.startswith("/dev/"):
        return ("remote", source)
    leaves: set[str] = set()
    pending = [os.path.realpath(source)]
    visited: set[str] = set()
    while pending:
        device = pending.pop()
        if device in visited:
            continue
        visited.add(device)
        output = _run_output(
            ["lsblk", "--noheadings", "--output", "PKNAME", device],
            label="lsblk",
        )
        parents = [line.strip() for line in output.splitlines() if line.strip()]
        if not parents:
            leaves.add(device)
        else:
            pending.extend("/dev/" + parent for parent in parents)
    if not leaves:
        raise OpsError("lsblk did not identify a physical backup device")
    return tuple(sorted(leaves))


def _validate_backup_storage(
    media_root: Path,
    backup_root: Path,
    *,
    storage_policy: Callable[[Path, Path], None] | None = None,
) -> None:
    if storage_policy is not None:
        storage_policy(media_root, backup_root)
        return
    media_source, _media_mount, _media_type = _mount_info(media_root)
    backup_source, _backup_mount, backup_type = _mount_info(backup_root, require_non_root_mount=True)
    if backup_type.casefold() in {"overlay", "tmpfs", "ramfs", "devtmpfs", "squashfs"}:
        raise OpsError("BACKUP_ROOT must use a durable mounted filesystem")
    if backup_source in {"", "-", "none"}:
        raise OpsError("BACKUP_ROOT mount source is unavailable")
    if _top_level_devices(media_source) == _top_level_devices(backup_source):
        raise OpsError("backup root must use a different top-level storage source")


def _native_postgres_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpsError("DATABASE_URL is required")
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg", "postgres"}:
        raise OpsError("DATABASE_URL must use PostgreSQL")
    scheme = "postgresql"
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _postgres_settings(value: str) -> dict[str, str | None]:
    native = _native_postgres_url(value)
    parsed = urlsplit(native)
    try:
        port = parsed.port
    except ValueError as exc:
        raise OpsError("PostgreSQL URL has an invalid port") from exc
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        raise OpsError("PostgreSQL URL must include a database")
    query = parse_qs(parsed.query, keep_blank_values=False)
    settings: dict[str, str | None] = {
        "PGHOST": parsed.hostname,
        "PGPORT": str(port or 5432),
        "PGUSER": unquote(parsed.username) if parsed.username is not None else None,
        "PGDATABASE": database,
        "PGPASSWORD": unquote(parsed.password) if parsed.password is not None else None,
    }
    for key in _PG_ENV_QUERY_KEYS:
        values = query.get(key)
        if values:
            settings["PG" + key.upper()] = unquote(values[0])
    return settings


def _pgpass_escape(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise OpsError("PostgreSQL credentials must not contain newlines")
    return value.replace("\\", "\\\\").replace(":", "\\:")


@contextmanager
def _postgres_environment(value: str, *, base: dict[str, str] | None = None) -> Iterator[dict[str, str]]:
    settings = _postgres_settings(value)
    environment = dict(os.environ if base is None else base)
    for key in {"PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD", "PGPASSFILE", "PGHOSTADDR"}:
        environment.pop(key, None)
    password = settings.pop("PGPASSWORD")
    for key, setting in settings.items():
        if setting is not None:
            environment[key] = setting
    passfile: Path | None = None
    if password is not None:
        escaped_user = _pgpass_escape(settings.get("PGUSER") or "*")
        escaped_password = _pgpass_escape(password)
        try:
            descriptor, filename = tempfile.mkstemp(prefix="before-after-pgpass-")
            passfile = Path(filename)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write("*:*:*:" + escaped_user + ":" + escaped_password + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            environment["PGPASSFILE"] = str(passfile)
        except OSError as exc:
            if passfile is not None:
                try:
                    passfile.unlink()
                except OSError:
                    pass
            raise OpsError("could not create protected PostgreSQL password file") from exc
    try:
        yield environment
    finally:
        if passfile is not None:
            try:
                passfile.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _fsync_file(path: Path, *, label: str) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise OpsError(f"could not open {label}") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise OpsError(f"could not fsync {label}") from exc
    finally:
        os.close(descriptor)


def _validate_postgres_dump(path: Path, *, pg_restore: str, env: dict[str, str]) -> None:
    _private_file(path, label="PostgreSQL dump")
    try:
        with path.open("rb") as stream:
            if stream.read(len(POSTGRES_DUMP_MAGIC)) != POSTGRES_DUMP_MAGIC:
                raise OpsError("PostgreSQL dump is not a custom-format PGDMP file")
    except OpsError:
        raise
    except OSError as exc:
        raise OpsError("could not inspect PostgreSQL dump") from exc
    _run_native([pg_restore, "--list", str(path)], label="pg_restore validation", env=env)


def _run_native(command: list[str], *, label: str, env: dict[str, str] | None = None) -> None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise OpsError(f"{label} is unavailable") from exc
    except OSError as exc:
        raise OpsError(f"{label} could not be started") from exc
    if completed.returncode != 0:
        # Do not include native-client stderr: it may echo a connection string
        # or a database/role name containing clinical information.
        raise OpsError(f"{label} failed with exit status {completed.returncode}")


def _sha256_file(path: Path) -> tuple[int, str]:
    _private_file(path)
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


def _safe_relative_path(path: Path) -> str:
    value = path.as_posix()
    pure = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise OpsError("backup manifest contains an unsafe path")
    return value


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise OpsError("could not open backup directory") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise OpsError("could not fsync backup directory") from exc
    finally:
        os.close(fd)


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    source_media: bool = False,
) -> tuple[int, str]:
    _private_file(source, label="media source", media=source_media)
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
    except FileExistsError as exc:
        raise OpsError("backup destination file collision") from exc
    except OSError as exc:
        raise OpsError("could not copy media into backup staging") from exc
    return _sha256_file(destination)


def _copy_media(source_root: Path, staging_media: Path) -> list[dict[str, object]]:
    _private_directory(source_root, create=False, media=True)
    _ensure_private_output_directory(staging_media)
    entries: list[dict[str, object]] = []
    for directory_name in MEDIA_DIRS:
        source_directory = source_root / directory_name
        destination_directory = staging_media / directory_name
        _private_directory(source_directory, create=False, media=True)
        _ensure_private_output_directory(destination_directory)
        for current, directories, files in os.walk(source_directory, topdown=True, followlinks=False):
            current_path = Path(current)
            _private_directory(current_path, create=False, media=True)
            for name in directories:
                path = current_path / name
                if os.path.islink(path):
                    raise OpsError("symlinks are not allowed in media backups")
                _private_directory(path, create=False, media=True)
            for name in files:
                source_path = current_path / name
                if os.path.islink(source_path):
                    raise OpsError("symlinks are not allowed in media backups")
                if not source_path.is_file():
                    raise OpsError("media backup accepts regular files only")
                relative = source_path.relative_to(source_root)
                relative_string = _safe_relative_path(Path("media") / relative)
                destination = staging_media / relative
                size, digest = _copy_regular_file(source_path, destination, source_media=True)
                entries.append({"path": relative_string, "bytes": size, "sha256": digest})
    return sorted(entries, key=lambda item: str(item["path"]))


def _write_json(path: Path, value: dict[str, object]) -> None:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="ascii") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        raise OpsError("backup manifest collision") from exc
    except OSError as exc:
        raise OpsError("could not publish backup manifest") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _generation_name() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{uuid.uuid4().hex}"


def _validate_generation_name(name: str) -> None:
    if GENERATION_RE.fullmatch(name) is None:
        raise OpsError("backup generation name is invalid")


def _cleanup_staging(root: Path) -> None:
    """Remove unpublished staging trees; they are never restore candidates."""
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise OpsError("could not list backup staging") from exc
    for entry in entries:
        if not entry.name.startswith(".staging-"):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir():
                entry.unlink()
            else:
                shutil.rmtree(entry)
        except OSError as exc:
            raise OpsError("could not remove stale backup staging") from exc
    _fsync_directory(root)


def create_backup(
    *,
    media_root: str | os.PathLike[str],
    backup_root: str | os.PathLike[str],
    database_url: str,
    pg_dump: str = "pg_dump",
    pg_restore: str = "pg_restore",
    retention: int | None = None,
    _storage_policy: Callable[[Path, Path], None] | None = None,
) -> BackupResult:
    """Create and atomically publish one paired backup generation.

    ``_storage_policy`` is an internal test seam. Production callers must use
    the mounted-filesystem and physical-device policy above.
    """
    source = _private_directory(_path(media_root, "MEDIA_ROOT"), create=False, media=True)
    destination_root = _backup_root(_path(backup_root, "BACKUP_ROOT"))
    if _path_overlap(source, destination_root):
        raise OpsError("backup root must not contain or be contained by MEDIA_ROOT")
    _validate_backup_storage(source, destination_root, storage_policy=_storage_policy)
    _cleanup_staging(destination_root)
    generation = _generation_name()
    _validate_generation_name(generation)
    staging = destination_root / f".staging-{uuid.uuid4().hex}"
    generation_path = destination_root / generation
    old_umask = os.umask(0o077)
    try:
        staging.mkdir(mode=0o700)
        _fsync_directory(destination_root)
        staging_media = staging / "media"
        media_entries = _copy_media(source, staging_media)
        dump_path = staging / DATABASE_DUMP_NAME
        command = [
            pg_dump,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(dump_path),
        ]
        with _postgres_environment(database_url) as database_environment:
            _run_native(command, label="pg_dump", env=database_environment)
            try:
                os.lstat(dump_path)
                if os.path.islink(dump_path) or not os.path.isfile(dump_path):
                    raise OpsError("PostgreSQL dump must be a regular file")
                os.chmod(dump_path, 0o600)
            except OpsError:
                raise
            except OSError as exc:
                raise OpsError("PostgreSQL dump is not usable") from exc
            _private_file(dump_path, label="PostgreSQL dump")
            _fsync_file(dump_path, label="PostgreSQL dump")
            _validate_postgres_dump(dump_path, pg_restore=pg_restore, env=database_environment)
        dump_size, dump_digest = _sha256_file(dump_path)
        files = [
            {"path": DATABASE_DUMP_NAME, "bytes": dump_size, "sha256": dump_digest},
            *media_entries,
        ]
        manifest: dict[str, object] = {
            "format_version": MANIFEST_FORMAT_VERSION,
            "complete": True,
            "verified": True,
            "generation": generation,
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "database": {"path": DATABASE_DUMP_NAME, "format": "custom"},
            "media": {"path": "media", "directories": list(MEDIA_DIRS)},
            "files": files,
        }
        _write_json(staging / MANIFEST_NAME, manifest)
        _private_directory(staging, create=False)
        _private_file(staging / MANIFEST_NAME, label="backup manifest")
        _fsync_directory(staging)
        if generation_path.exists() or os.path.lexists(generation_path):
            raise OpsError("backup generation collision")
        os.replace(staging, generation_path)
        os.chmod(generation_path, 0o700)
        _fsync_directory(destination_root)
        if retention is not None:
            prune_generations(destination_root, retain=retention, _protected_generation=generation)
        return BackupResult(generation=generation, path=generation_path, manifest=manifest)
    except OpsError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise OpsError("backup generation could not be published") from exc
    finally:
        os.umask(old_umask)
        if staging.exists() or os.path.lexists(staging):
            try:
                if staging.is_dir() and not os.path.islink(staging):
                    shutil.rmtree(staging)
            except OSError:
                pass


def _generation_is_verified(generation: Path) -> bool:
    """Return true only for a private, complete generation with valid files."""
    try:
        _private_directory(generation, create=False)
        _validate_generation_name(generation.name)
        manifest_path = generation / MANIFEST_NAME
        _private_file(manifest_path, label="backup manifest")
        with manifest_path.open(encoding="ascii") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict) or manifest.get("complete") is not True:
            return False
        if manifest.get("generation") != generation.name:
            return False
        raw_files = manifest.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            return False
        expected: set[str] = set()
        for raw in raw_files:
            if not isinstance(raw, dict):
                return False
            relative = _safe_relative_path(Path(str(raw.get("path", ""))))
            if relative in expected:
                return False
            size = raw.get("bytes")
            digest = raw.get("sha256")
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                return False
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                return False
            if relative != DATABASE_DUMP_NAME and not relative.startswith("media/"):
                return False
            file_path = generation / Path(*PurePosixPath(relative).parts)
            _assert_no_symlink_components(file_path, allow_missing_leaf=False)
            actual_size, actual_digest = _sha256_file(file_path)
            if (actual_size, actual_digest) != (size, digest):
                return False
            expected.add(relative)
        if DATABASE_DUMP_NAME not in expected:
            return False
        actual = set()
        for current, directories, files in os.walk(generation, topdown=True, followlinks=False):
            current_path = Path(current)
            _private_directory(current_path, create=False)
            for directory in directories:
                _private_directory(current_path / directory, create=False)
            for filename in files:
                file_path = current_path / filename
                _private_file(file_path, label="backup file")
                relative = file_path.relative_to(generation).as_posix()
                if relative != MANIFEST_NAME:
                    actual.add(relative)
        return actual == expected
    except (OSError, OpsError, UnicodeError, ValueError, TypeError):
        return False


def generation_names(backup_root: str | os.PathLike[str]) -> list[str]:
    root = _backup_root(_path(backup_root, "BACKUP_ROOT"))
    _cleanup_staging(root)
    result: list[str] = []
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise OpsError("could not list backup generations") from exc
    for entry in entries:
        if GENERATION_RE.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink():
            result.append(entry.name)
    return sorted(result)


def prune_generations(
    backup_root: str | os.PathLike[str],
    *,
    retain: int,
    _protected_generation: str | None = None,
) -> list[str]:
    """Delete old generations without removing the last verified generation."""
    if isinstance(retain, bool) or not isinstance(retain, int) or retain < 1:
        raise OpsError("backup retention must be a positive integer")
    root = _backup_root(_path(backup_root, "BACKUP_ROOT"))
    names = generation_names(root)
    verified = [name for name in names if _generation_is_verified(root / name)]
    newest_verified = verified[-1] if verified else None
    protected = _protected_generation if _protected_generation in verified else newest_verified
    removed: list[str] = []
    for name in names[:-retain]:
        if name == protected or not _generation_is_verified(root / name):
            continue
        generation = root / name
        try:
            shutil.rmtree(generation)
        except OSError as exc:
            raise OpsError("could not prune backup generation") from exc
        removed.append(name)
    if removed:
        _fsync_directory(root)
    return removed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-root", default=os.environ.get("MEDIA_ROOT"))
    parser.add_argument("--backup-root", default=os.environ.get("BACKUP_ROOT"))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--pg-dump", default=os.environ.get("PG_DUMP", "pg_dump"))
    parser.add_argument("--pg-restore", default=os.environ.get("PG_RESTORE", "pg_restore"))
    parser.add_argument(
        "--retain",
        type=int,
        default=(int(os.environ["BACKUP_RETENTION_GENERATIONS"]) if os.environ.get("BACKUP_RETENTION_GENERATIONS") else None),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = create_backup(
            media_root=args.media_root,
            backup_root=args.backup_root,
            database_url=args.database_url,
            pg_dump=args.pg_dump,
            pg_restore=args.pg_restore,
            retention=args.retain,
        )
    except OpsError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"backup generation published: {result.generation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
