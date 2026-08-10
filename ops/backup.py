#!/usr/bin/env python3
"""Create an atomic PostgreSQL/media backup generation.

This module deliberately has no Flask imports.  It is run by a scheduled
service while the application is stopped by ``ops/backup.sh``.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
from datetime import datetime, timezone
import fnmatch
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
import time
from typing import Callable, Iterator
import uuid

from app.db import postgres_route, ops_postgres_environment, credential_free_database_url


GENERATION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$")
MEDIA_DIRS = ("originals", "previews", "derivatives", "quarantine")
MANIFEST_NAME = "manifest.json"
DATABASE_DUMP_NAME = "database.dump"
MANIFEST_FORMAT_VERSION = 2
POSTGRES_DUMP_MAGIC = b"PGDMP"
_BACKUP_LOCK_NAME = ".backup.lock"
_STAGING_OWNER_NAME = ".staging-owner.json"
_STAGING_OWNER_FORMAT = "before-after.backup-staging.v1"
_STAGING_RE = re.compile(r"^\.staging-([0-9a-f]{32})$")
POSTGRES_CLIENTS = ("pg_dump", "pg_restore", "psql")
_POSIX_ACL_XATTRS = frozenset({"system.posix_acl_access", "system.posix_acl_default"})
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


def _postgres_major_from_text(output: str, *, label: str) -> int:
    """Extract a PostgreSQL major from a client/server version response."""
    match = re.search(r"(?<!\d)(\d+)(?:(?:\.\d+)+|devel|beta\d*|rc\d*)?", output)
    if match is None:
        raise OpsError(f"{label} returned an invalid PostgreSQL version")
    major = int(match.group(1))
    if major < 9 or major > 99:
        raise OpsError(f"{label} returned an invalid PostgreSQL major")
    return major


def _postgres_client_major(command: str, *, label: str) -> int:
    try:
        completed = subprocess.run(
            [command, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        raise OpsError(f"{label} is unavailable") from exc
    if completed.returncode != 0:
        raise OpsError(f"{label} version check failed with exit status {completed.returncode}")
    return _postgres_major_from_text(completed.stdout or completed.stderr, label=label)


def _postgres_server_major(database_url: str, *, psql: str = "psql") -> int:
    """Read the actual server major through the native psql client."""
    with _postgres_environment(database_url) as environment:
        try:
            completed = subprocess.run(
                [psql, "--no-psqlrc", "--tuples-only", "--no-align", "--command", "SHOW server_version_num"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
        except (FileNotFoundError, OSError) as exc:
            raise OpsError("psql is unavailable") from exc
    if completed.returncode != 0:
        raise OpsError(f"PostgreSQL server version check failed with exit status {completed.returncode}")
    value = completed.stdout.strip()
    if value.isdigit() and len(value) >= 5:
        major = int(value) // 10000
        if 9 <= major <= 99:
            return major
    return _postgres_major_from_text(value, label="PostgreSQL server")


def postgres_preflight(
    database_url: str,
    *,
    pg_dump: str = "pg_dump",
    pg_restore: str = "pg_restore",
    psql: str = "psql",
) -> dict[str, object]:
    """Require matching native clients and the connected server major."""
    clients = {
        "pg_dump": _postgres_client_major(pg_dump, label="pg_dump"),
        "pg_restore": _postgres_client_major(pg_restore, label="pg_restore"),
        "psql": _postgres_client_major(psql, label="psql"),
    }
    client_majors = set(clients.values())
    if len(client_majors) != 1:
        raise OpsError("PostgreSQL native client major versions do not match")
    server_major = _postgres_server_major(database_url, psql=psql)
    client_major = next(iter(client_majors))
    if client_major != server_major:
        raise OpsError(
            f"PostgreSQL client major {client_major} does not match server major {server_major}"
        )
    return {"server_major": server_major, "client_majors": clients}


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
    try:
        return credential_free_database_url(value)
    except RuntimeError as exc:
        raise OpsError(str(exc)) from exc


def _postgres_settings(value: str) -> dict[str, str | None]:
    try:
        return dict(postgres_route(value).environment())
    except RuntimeError as exc:
        raise OpsError(str(exc)) from exc


def _pgpass_escape(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise OpsError("PostgreSQL credentials must not contain newlines")
    return value.replace("\\", "\\\\").replace(":", "\\:")


@contextmanager
def _postgres_environment(value: str, *, base: dict[str, str] | None = None) -> Iterator[dict[str, str]]:
    try:
        route = postgres_route(value)
        settings = dict(route.environment())
        environment = ops_postgres_environment(
            value,
            base=base,
            include_database_url=False,
            create_passfile=False,
        )
    except RuntimeError as exc:
        raise OpsError(str(exc)) from exc
    password = route._password
    existing_passfile = environment.get("PGPASSFILE")
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
    elif existing_passfile:
        environment["PGPASSFILE"] = existing_passfile
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


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:
        raise OpsError("descriptor-based media backup requires POSIX no-follow primitives") from exc


def _file_flags() -> int:
    try:
        return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    except AttributeError as exc:
        raise OpsError("descriptor-based media backup requires POSIX no-follow primitives") from exc


def _open_directory_path(path: Path) -> int:
    """Open a managed directory component-by-component without following links."""
    descriptor: int | None = None
    completed = False
    try:
        descriptor = os.open("/", _directory_flags())
        for part in path.absolute().parts[1:]:
            if part in {"", "."}:
                continue
            if part == "..":
                raise OpsError("managed media path contains traversal")
            try:
                before = os.lstat(part, dir_fd=descriptor)
                if not stat.S_ISDIR(before.st_mode):
                    raise OpsError("managed media path contains a non-directory")
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            except OpsError:
                raise
            except OSError as exc:
                raise OpsError("managed media path is not safely openable") from exc
            try:
                after = os.fstat(child)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise OpsError("managed media path changed identity")
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        completed = True
        return descriptor
    except OpsError:
        raise
    except OSError as exc:
        raise OpsError("managed media path is not safely openable") from exc
    finally:
        if descriptor is not None and not completed:
            os.close(descriptor)


def _media_directory_info(info: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise OpsError(f"{label} must be a directory")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o007 or mode & 0o020 or mode & 0o070 != 0o050:
        raise OpsError(f"{label} must be group-readable and not writable")


def _assert_no_posix_acl_fd(fd: int, label: str) -> None:
    try:
        names = os.listxattr(fd)
    except AttributeError:
        return
    except OSError as exc:
        if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}:
            return
        raise OpsError(f"could not inspect ACLs for {label}") from exc
    if _POSIX_ACL_XATTRS.intersection(
        name.decode() if isinstance(name, bytes) else name for name in names
    ):
        raise OpsError(f"POSIX ACLs are not allowed in media: {label}")


def _media_file_info(info: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise OpsError(f"{label} must be a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o007 or mode & 0o020 or mode & 0o040 != 0o040:
        raise OpsError(f"{label} must be group-readable and not writable")


def _entry_lstat(parent_fd: int, name: str) -> os.stat_result:
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except OSError as exc:
        raise OpsError("could not inspect media entry") from exc
    if stat.S_ISLNK(info.st_mode):
        raise OpsError("symlinks are not allowed in media backups")
    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
        raise OpsError("media backup accepts regular files and directories only")
    return info


def _open_media_entry(parent_fd: int, name: str, *, directory: bool, before: os.stat_result) -> int:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(before.st_mode):
        raise OpsError("media entry changed type")
    try:
        descriptor = os.open(
            name,
            _directory_flags() if directory else _file_flags(),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        try:
            current = os.lstat(name, dir_fd=parent_fd)
        except OSError:
            current = None
        if current is not None and (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise OpsError("media entry changed identity") from exc
        raise OpsError("could not safely open media entry") from exc
    try:
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise OpsError("media entry changed identity")
        if not expected(after.st_mode):
            raise OpsError("media entry changed type")
        _assert_no_posix_acl_fd(descriptor, name)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _copy_regular_descriptor(
    source_fd: int,
    destination: Path,
    source_info: os.stat_result,
) -> tuple[int, str]:
    _assert_no_posix_acl_fd(source_fd, "media source")
    _assert_no_symlink_components(destination.parent)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink_components(destination.parent, allow_missing_leaf=False)
    os.chmod(destination.parent, 0o700)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(os.dup(source_fd), "rb") as source_stream, os.fdopen(descriptor, "wb") as destination_stream:
            descriptor = None
            while chunk := source_stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
                destination_stream.write(chunk)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
        after = os.fstat(source_fd)
        if (
            (after.st_dev, after.st_ino) != (source_info.st_dev, source_info.st_ino)
            or not stat.S_ISREG(after.st_mode)
            or after.st_size != source_info.st_size
        ):
            raise OpsError("media source changed while it was copied")
        if size <= 0:
            raise OpsError("media backup rejects empty sources")
        return size, digest.hexdigest()
    except FileExistsError as exc:
        raise OpsError("backup destination file collision") from exc
    except OpsError:
        raise
    except OSError as exc:
        raise OpsError("could not copy media into backup staging") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _copy_media_tree(
    source_fd: int,
    destination: Path,
    relative: Path,
    entries: list[dict[str, object]],
) -> None:
    _media_directory_info(os.fstat(source_fd), "media source directory")
    _assert_no_posix_acl_fd(source_fd, "media source directory")
    try:
        with os.scandir(source_fd) as source_entries:
            names = [entry.name for entry in source_entries]
    except OSError as exc:
        raise OpsError("could not list media source") from exc
    for name in names:
        before = _entry_lstat(source_fd, name)
        if _is_recovery_marker(name):
            raise OpsError(
                "recovery required: MEDIA_ROOT contains an active recovery marker; "
                "reconcile it as before-after before retrying the backup"
            )
        child_relative = relative / name
        if stat.S_ISDIR(before.st_mode):
            child_fd = _open_media_entry(source_fd, name, directory=True, before=before)
            child_destination = destination / name
            try:
                _media_directory_info(os.fstat(child_fd), "media source directory")
                _ensure_private_output_directory(child_destination)
                _copy_media_tree(child_fd, child_destination, child_relative, entries)
            finally:
                os.close(child_fd)
            continue
        child_fd = _open_media_entry(source_fd, name, directory=False, before=before)
        try:
            source_info = os.fstat(child_fd)
            _media_file_info(source_info, "media source")
            size, digest = _copy_regular_descriptor(
                child_fd,
                destination / name,
                source_info,
            )
        finally:
            os.close(child_fd)
        entries.append(
            {
                "path": _safe_relative_path(Path("media") / child_relative),
                "bytes": size,
                "sha256": digest,
            }
        )


def _copy_media(source_root: Path, staging_media: Path) -> list[dict[str, object]]:
    _private_directory(source_root, create=False, media=True)
    _ensure_private_output_directory(staging_media)
    source_fd = _open_directory_path(source_root)
    entries: list[dict[str, object]] = []
    try:
        _media_directory_info(os.fstat(source_fd), "MEDIA_ROOT")
        _assert_no_posix_acl_fd(source_fd, "MEDIA_ROOT")
        try:
            with os.scandir(source_fd) as source_entries:
                root_names = [entry.name for entry in source_entries]
        except OSError as exc:
            raise OpsError("could not list MEDIA_ROOT") from exc
        if not set(MEDIA_DIRS).issubset(root_names):
            raise OpsError("MEDIA_ROOT is missing a managed directory")
        for name in root_names:
            before = _entry_lstat(source_fd, name)
            if name in MEDIA_DIRS:
                if not stat.S_ISDIR(before.st_mode):
                    raise OpsError("managed media directory changed type")
                child_fd = _open_media_entry(source_fd, name, directory=True, before=before)
                try:
                    _media_directory_info(os.fstat(child_fd), name)
                    destination = staging_media / name
                    _ensure_private_output_directory(destination)
                    _copy_media_tree(child_fd, destination, Path(name), entries)
                finally:
                    os.close(child_fd)
                continue
            if name == ".reconcile.lock":
                if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600:
                    raise OpsError("reconciliation lock has unsafe permissions or type")
                continue
            if name == ".backup.lock":
                mode = stat.S_IMODE(before.st_mode)
                if not stat.S_ISREG(before.st_mode) or mode != 0o660:
                    raise OpsError("MEDIA_ROOT coordination lock has unsafe permissions or type")
                continue
            if _is_recovery_marker(name):
                raise OpsError("recovery required: MEDIA_ROOT changed during backup")
            raise OpsError("MEDIA_ROOT contains unmanaged content")
    finally:
        os.close(source_fd)
    return sorted(entries, key=lambda item: str(item["path"]))


def _is_recovery_marker(name: str) -> bool:
    return (
        name.startswith(".pending-")
        or name.startswith(".capture-delete-")
        or name.startswith(".restore-")
        or fnmatch.fnmatchcase(name, ".upload-*.tmp")
    )


def _reject_recovery_marker(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise OpsError("recovery required: could not inspect an active recovery marker") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise OpsError("recovery required: active recovery marker has unsafe permissions or type")
    raise OpsError(
        "recovery required: MEDIA_ROOT contains an active recovery marker; "
        "reconcile it as before-after before retrying the backup"
    )


def _snapshot_media_directory(fd: int, *, root: bool) -> None:
    """Validate the stopped media tree through directory descriptors only."""
    _media_directory_info(os.fstat(fd), "MEDIA_ROOT" if root else "media directory")
    _assert_no_posix_acl_fd(fd, "MEDIA_ROOT" if root else "media directory")
    try:
        with os.scandir(fd) as source_entries:
            names = [entry.name for entry in source_entries]
    except OSError as exc:
        raise OpsError("could not list MEDIA_ROOT") from exc
    if root and not set(MEDIA_DIRS).issubset(names):
        raise OpsError("MEDIA_ROOT is missing a managed directory")
    for name in names:
        before = _entry_lstat(fd, name)
        is_directory = stat.S_ISDIR(before.st_mode)
        if root and is_directory and name not in MEDIA_DIRS:
            raise OpsError("MEDIA_ROOT contains unmanaged content")
        if root and not is_directory:
            if name == ".reconcile.lock":
                _private_mode = stat.S_IMODE(before.st_mode)
                if not stat.S_ISREG(before.st_mode) or _private_mode != 0o600:
                    raise OpsError("reconciliation lock has unsafe permissions or type")
                continue
            if name == ".backup.lock":
                mode = stat.S_IMODE(before.st_mode)
                if not stat.S_ISREG(before.st_mode) or mode != 0o660:
                    raise OpsError("MEDIA_ROOT coordination lock has unsafe permissions or type")
                continue
            if _is_recovery_marker(name):
                raise OpsError(
                    "recovery required: MEDIA_ROOT contains an active recovery marker; "
                    "reconcile it as before-after before retrying the backup"
                )
            raise OpsError("MEDIA_ROOT contains unmanaged content")
        if not is_directory:
            if _is_recovery_marker(name):
                raise OpsError(
                    "recovery required: MEDIA_ROOT contains an active recovery marker; "
                    "reconcile it as before-after before retrying the backup"
                )
            _media_file_info(before, "media source")
            continue
        child_fd = _open_media_entry(fd, name, directory=True, before=before)
        try:
            _snapshot_media_directory(child_fd, root=False)
        finally:
            os.close(child_fd)


def _assert_media_snapshot_ready(source_root: Path) -> None:
    """Refuse a snapshot that would omit an in-flight media operation.

    Recovery markers are application-private and intentionally not group
    readable. The backup identity cannot safely reconcile them, so the
    stopped-app precondition fails closed instead of copying a mixed pair.
    """
    _private_directory(source_root, create=False, media=True)
    source_fd = _open_directory_path(source_root)
    try:
        _snapshot_media_directory(source_fd, root=True)
    finally:
        os.close(source_fd)


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


@contextmanager
def _backup_operation_lock(
    root: Path,
    *,
    lock_path: Path | None = None,
) -> Iterator[None]:
    """Serialize backup publication and explicitly requested stale cleanup."""
    lock_path = lock_path or root / _BACKUP_LOCK_NAME
    descriptor: int | None = None
    inherited_fd = os.environ.get("BACKUP_LOCK_FD") if lock_path.name == ".backup.lock" else None
    try:
        if inherited_fd is not None:
            try:
                descriptor = int(inherited_fd)
                info = os.fstat(descriptor)
                expected = os.lstat(lock_path)
                if (
                    not stat.S_ISREG(expected.st_mode)
                    or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)
                ):
                    raise ValueError
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OpsError("backup operation lock is already held") from exc
            except (OSError, ValueError) as exc:
                raise OpsError("inherited backup lock is unsafe") from exc
            yield
            return
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o660 if lock_path.name == ".backup.lock" else 0o600,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OpsError("backup operation lock is unsafe")
        os.fchmod(descriptor, 0o660 if lock_path.name == ".backup.lock" else 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OpsError:
        raise
    except OSError as exc:
        raise OpsError("could not acquire backup operation lock") from exc
    finally:
        if descriptor is not None and inherited_fd is None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _staging_owner(staging: Path, run_id: str) -> None:
    _write_json(
        staging / _STAGING_OWNER_NAME,
        {
            "format": _STAGING_OWNER_FORMAT,
            "run_id": run_id,
            "pid": os.getpid(),
            "created_ns": time.time_ns(),
        },
    )


def _staging_is_stale(entry: Path, *, now: float, age_seconds: float) -> bool:
    match = _STAGING_RE.fullmatch(entry.name)
    if match is None or entry.is_symlink() or not entry.is_dir():
        return False
    try:
        age = now - entry.stat().st_mtime
        owner = entry / _STAGING_OWNER_NAME
        _private_file(owner, label="backup staging owner marker")
        value = json.loads(owner.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return False
    return (
        age >= age_seconds
        and isinstance(value, dict)
        and value.get("format") == _STAGING_OWNER_FORMAT
        and value.get("run_id") == match.group(1)
        and isinstance(value.get("pid"), int)
        and not isinstance(value.get("pid"), bool)
        and isinstance(value.get("created_ns"), int)
        and not isinstance(value.get("created_ns"), bool)
        and value["created_ns"] > 0
        and value["created_ns"] / 1_000_000_000 <= now
    )


def _cleanup_staging_locked(root: Path, *, age_seconds: float, now: float | None = None) -> list[str]:
    if isinstance(age_seconds, bool) or not isinstance(age_seconds, (int, float)) or age_seconds < 0:
        raise OpsError("stale staging age must be non-negative")
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise OpsError("could not list backup staging") from exc
    removed: list[str] = []
    current_time = time.time() if now is None else now
    for entry in entries:
        if not _staging_is_stale(entry, now=current_time, age_seconds=float(age_seconds)):
            continue
        try:
            _remove_private_path_strict(entry)
        except OpsError as exc:
            raise OpsError("could not remove stale backup staging") from exc
        removed.append(entry.name)
    if removed:
        _fsync_directory(root)
    return removed


def cleanup_stale_staging(
    backup_root: str | os.PathLike[str],
    *,
    age_seconds: float = 24 * 60 * 60,
    now: float | None = None,
) -> list[str]:
    """Remove only old, owner-marked unpublished stages under the backup lock."""
    root = _backup_root(_path(backup_root, "BACKUP_ROOT"))
    with _backup_operation_lock(root):
        return _cleanup_staging_locked(root, age_seconds=age_seconds, now=now)


def _remove_private_path_strict(path: Path) -> None:
    """Remove a backup staging artifact, surfacing a poisoned cleanup."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OpsError("could not inspect backup staging cleanup path") from exc
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
        try:
            if path.is_dir() and not path.is_symlink():
                diagnostic = path / ".cleanup-failed"
                diagnostic.write_text("backup staging cleanup failed; manual removal required\n", encoding="ascii")
                os.chmod(diagnostic, 0o600)
                _fsync_directory(path)
        except OSError:
            pass
        raise OpsError("could not remove backup staging; staging is poisoned") from exc


def create_backup(
    *,
    media_root: str | os.PathLike[str],
    backup_root: str | os.PathLike[str],
    database_url: str,
    pg_dump: str = "pg_dump",
    pg_restore: str = "pg_restore",
    psql: str = "psql",
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
    operation_lock = _backup_operation_lock(
        destination_root,
        lock_path=source / ".backup.lock",
    )
    operation_lock.__enter__()
    staging: Path | None = None
    old_umask = os.umask(0o077)
    try:
        _assert_media_snapshot_ready(source)
        postgres_metadata = (
            postgres_preflight(
                database_url,
                pg_dump=pg_dump,
                pg_restore=pg_restore,
                psql=psql,
            )
            if _storage_policy is None or (pg_dump, pg_restore, psql) == ("pg_dump", "pg_restore", "psql")
            else {"test_only": True}
        )
        _cleanup_staging_locked(destination_root, age_seconds=24 * 60 * 60)
        generation = _generation_name()
        _validate_generation_name(generation)
        staging = destination_root / f".staging-{uuid.uuid4().hex}"
        generation_path = destination_root / generation
        staging.mkdir(mode=0o700)
        _staging_owner(staging, staging.name.removeprefix(".staging-"))
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
            "postgresql": postgres_metadata,
            "media": {"path": "media", "directories": list(MEDIA_DIRS)},
            "files": files,
        }
        _write_json(staging / MANIFEST_NAME, manifest)
        _private_directory(staging, create=False)
        _private_file(staging / MANIFEST_NAME, label="backup manifest")
        if not _generation_is_verified(
            staging,
            expected_name=generation,
            allow_staging_owner=True,
        ):
            raise OpsError("backup generation content verification failed")
        try:
            (staging / _STAGING_OWNER_NAME).unlink()
        except OSError as exc:
            raise OpsError("could not finalize backup staging owner marker") from exc
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
        try:
            if staging is not None and (staging.exists() or os.path.lexists(staging)):
                try:
                    _remove_private_path_strict(staging)
                except OpsError as cleanup_exc:
                    raise OpsError(
                        "backup failed and staging cleanup failed; clinical staging is poisoned"
                    ) from cleanup_exc
        finally:
            operation_lock.__exit__(None, None, None)


def _generation_is_verified(
    generation: Path,
    *,
    expected_name: str | None = None,
    allow_staging_owner: bool = False,
) -> bool:
    """Return true only for the same restorable-v2 verifier used by restore."""
    try:
        from ops.restore_check import verify_generation

        verify_generation(
            generation,
            expected_name=expected_name,
            allow_staging_owner=allow_staging_owner,
        )
        return True
    except (ImportError, OSError, OpsError, UnicodeError, ValueError, TypeError):
        return False


def generation_names(backup_root: str | os.PathLike[str]) -> list[str]:
    root = _backup_root(_path(backup_root, "BACKUP_ROOT"))
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
    keep = set(verified[-retain:])
    if protected is not None and protected not in keep:
        if keep:
            keep.remove(min(keep))
        keep.add(protected)
    removed: list[str] = []
    for name in verified:
        if name in keep:
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
    parser.add_argument("--psql", default=os.environ.get("PSQL", "psql"))
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
            psql=args.psql,
            retention=args.retain,
        )
    except OpsError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"backup generation published: {result.generation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
