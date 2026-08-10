#!/usr/bin/env python3
"""Normalize or verify the managed media tree without pathname races."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import grp
import os
from pathlib import Path
import pwd
import re
import stat
import sys


MANAGED_DIRECTORIES = ("originals", "previews", "derivatives", "quarantine")
ROOT_MODE = 0o2750
DIRECTORY_MODE = 0o750
MEDIA_MODE = 0o640
PRIVATE_MODE = 0o600
BACKUP_LOCK_MODE = 0o660
_MARKER_PATTERNS = (
    re.compile(r"^\.pending-[0-9a-f]{32}(?:\.tmp)?$"),
    re.compile(r"^\.capture-delete-[0-9a-f]{32}(?:\.tmp)?$"),
    re.compile(r"^\.upload-.+\.tmp$"),
    re.compile(r"^\.restore-.*$"),
)
_POSIX_ACL_XATTRS = frozenset({"system.posix_acl_access", "system.posix_acl_default"})
_DANGEROUS_ROOTS = frozenset(
    Path(path)
    for path in (
        "/",
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/var",
        "/opt",
        "/home",
        "/root",
        "/tmp",
        "/dev",
        "/proc",
        "/sys",
        "/run",
    )
)


class PermissionError(RuntimeError):
    """Raised when a media entry is missing or unsafe."""


@dataclass(frozen=True)
class _FileRecord:
    parent_fd: int
    name: str
    identity: tuple[int, int]
    mode: int


@dataclass(frozen=True)
class _DirectoryRecord:
    fd: int
    identity: tuple[int, int]
    mode: int


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:
        raise PermissionError("media permissions require POSIX no-follow primitives") from exc


def _file_flags() -> int:
    try:
        return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    except AttributeError as exc:
        raise PermissionError("media permissions require POSIX no-follow primitives") from exc


def _check_directory(fd: int, label: str) -> os.stat_result:
    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise PermissionError(f"could not inspect {label}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"{label} is not a directory")
    _check_no_posix_acl(fd, label)
    return info


def _check_no_posix_acl(fd: int, label: str) -> None:
    """Reject POSIX ACLs instead of silently widening mode-bit policy."""
    try:
        names = os.listxattr(fd)
    except AttributeError:
        return
    except OSError as exc:
        if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}:
            return
        raise PermissionError(f"could not inspect ACLs for {label}") from exc
    if _POSIX_ACL_XATTRS.intersection(
        name.decode() if isinstance(name, bytes) else name for name in names
    ):
        raise PermissionError(f"POSIX ACLs are not allowed in media: {label}")


def _validate_media_path(path: str) -> Path:
    if not isinstance(path, str) or "\x00" in path:
        raise PermissionError("MEDIA_ROOT must be an absolute path")
    candidate = Path(path)
    if not candidate.is_absolute() or candidate in _DANGEROUS_ROOTS:
        raise PermissionError("MEDIA_ROOT is not an approved managed directory")
    if any(part in {"", ".", ".."} for part in candidate.parts[1:]):
        raise PermissionError("MEDIA_ROOT must not contain traversal")
    return candidate


def _trusted_directory(info: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"{label} must be a directory")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise PermissionError(f"{label} must be root-owned and not group/world-writable")


def _open_root(path: str) -> int:
    candidate = _validate_media_path(path)
    fd: int | None = None
    try:
        fd = os.open("/", _directory_flags())
        for part in candidate.parts[1:]:
            if part in {"", "."}:
                continue
            if part == "..":
                raise PermissionError("MEDIA_ROOT must not contain traversal")
            child = _open_entry(fd, part, directory=True)
            _check_directory(child, f"MEDIA_ROOT component {part}")
            os.close(fd)
            fd = child
        _check_directory(fd, "MEDIA_ROOT")
        result = fd
        fd = None
        return result
    except PermissionError:
        raise
    except OSError as exc:
        raise PermissionError("MEDIA_ROOT contains a symlink or is not a directory") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _open_trusted_parent(
    path: str, *, create_missing: bool = False
) -> tuple[int, str, Path]:
    """Open the trusted parent chain, optionally creating safe missing parents."""
    candidate = _validate_media_path(path)
    parts = candidate.parts[1:]
    if not parts:
        raise PermissionError("MEDIA_ROOT is not an approved managed directory")
    descriptor = os.open("/", _directory_flags())
    try:
        for index, part in enumerate(parts[:-1]):
            try:
                before = os.lstat(part, dir_fd=descriptor)
            except FileNotFoundError:
                if not create_missing:
                    raise PermissionError("MEDIA_ROOT parent chain is incomplete")
                # Once a component is absent, later components cannot be
                # attacker-controlled entries. Create the remainder through
                # mkdirat/openat, under the already validated parent.
                for missing in parts[index:-1]:
                    try:
                        os.mkdir(missing, 0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = _open_entry(descriptor, missing, directory=True)
                    try:
                        _trusted_directory(os.fstat(child), f"MEDIA_ROOT parent {missing}")
                        os.fchown(child, 0, 0)
                        os.fchmod(child, 0o755)
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    descriptor = child
                return descriptor, parts[-1], candidate
            except OSError as exc:
                raise PermissionError("could not inspect MEDIA_ROOT parent chain") from exc
            _trusted_directory(before, f"MEDIA_ROOT parent {part}")
            child = _open_entry(descriptor, part, directory=True, before=before)
            _trusted_directory(os.fstat(child), f"MEDIA_ROOT parent {part}")
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1], candidate
    except Exception:
        os.close(descriptor)
        raise


def _preflight_tree(
    fd: int,
    *,
    root: bool,
    seen: set[tuple[int, int]],
) -> list[str]:
    """Validate types, ACLs, and the root allowlist before any mutation."""
    info = _check_directory(fd, "MEDIA_ROOT" if root else "media directory")
    identity = (info.st_dev, info.st_ino)
    if identity in seen:
        raise PermissionError("media directory inode was visited twice")
    seen.add(identity)
    try:
        with os.scandir(fd) as entries:
            names = [entry.name for entry in entries]
    except OSError as exc:
        raise PermissionError("could not list media directory") from exc
    for name in names:
        child_info = _entry_lstat(fd, name)
        is_directory = stat.S_ISDIR(child_info.st_mode)
        if root and not _root_name_allowed(name, directory=is_directory):
            raise PermissionError(f"MEDIA_ROOT contains unmanaged root entry: {name}")
        if not is_directory and not _file_name_allowed(name, root=root):
            raise PermissionError(f"unknown operational file in media: {name}")
        child_fd = _open_entry(fd, name, directory=is_directory, before=child_info)
        try:
            if is_directory:
                _preflight_tree(child_fd, root=False, seen=seen)
        finally:
            os.close(child_fd)
    return names


def _prepare(root: str, owner: str, group: str) -> None:
    """Create the managed topology only after descriptor-based preflight."""
    uid, gid = _resolve_ids(owner, group)
    parent_fd, leaf, candidate = _open_trusted_parent(root, create_missing=True)
    root_fd: int | None = None
    try:
        try:
            root_info = os.lstat(leaf, dir_fd=parent_fd)
        except FileNotFoundError:
            os.mkdir(leaf, ROOT_MODE, dir_fd=parent_fd)
            os.fsync(parent_fd)
            root_info = os.lstat(leaf, dir_fd=parent_fd)
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise PermissionError("MEDIA_ROOT must be a real directory")
        if root_info.st_uid not in {0, uid} or root_info.st_gid not in {0, gid}:
            raise PermissionError("MEDIA_ROOT has an unapproved owner or group")
        root_fd = _open_entry(parent_fd, leaf, directory=True, before=root_info)
        names = _preflight_tree(root_fd, root=True, seen=set())
        for name in MANAGED_DIRECTORIES:
            if name in names:
                continue
            os.mkdir(name, ROOT_MODE, dir_fd=root_fd)
            os.fsync(root_fd)
        if ".backup.lock" not in names:
            fd = os.open(
                ".backup.lock",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                BACKUP_LOCK_MODE,
                dir_fd=root_fd,
            )
            os.close(fd)
            os.fsync(root_fd)
        # Re-run the descriptor-only normalizer after creation. Existing
        # entries were fully preflighted before this point.
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)
    run(str(candidate), owner, group, mutate=True, require_backup_lock=True)


def _entry_lstat(parent_fd: int, name: str) -> os.stat_result:
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except OSError as exc:
        raise PermissionError(f"could not inspect media entry {name}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise PermissionError(f"symlinks are not allowed in media: {name}")
    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
        raise PermissionError(f"special files are not allowed in media: {name}")
    return info


def _open_entry(
    parent_fd: int,
    name: str,
    *,
    directory: bool,
    before: os.stat_result | None = None,
) -> int:
    before = _entry_lstat(parent_fd, name) if before is None else before
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(before.st_mode):
        raise PermissionError(f"media entry has unexpected type: {name}")
    try:
        fd = os.open(
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
            raise PermissionError(f"media entry changed identity: {name}") from exc
        raise PermissionError(
            f"could not safely open media entry {name}; changed identity or concurrent mutation was blocked"
        ) from exc
    try:
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise PermissionError(f"media entry changed identity: {name}")
        if not expected(after.st_mode):
            raise PermissionError(f"media entry changed type: {name}")
        _check_no_posix_acl(fd, name)
        return fd
    except Exception:
        os.close(fd)
        raise


def _fd_contained(fd: int, root: Path, label: str) -> None:
    """Reject an opened directory that escaped MEDIA_ROOT when procfs exists."""
    proc_fd = Path("/proc/self/fd")
    if not proc_fd.is_dir():
        return
    try:
        target = os.readlink(proc_fd / str(fd))
    except OSError as exc:
        raise PermissionError(f"could not verify {label} containment") from exc
    if target.endswith(" (deleted)"):
        raise PermissionError(f"{label} was removed during validation")
    try:
        actual = Path(target).resolve(strict=False)
        actual.relative_to(root)
    except (OSError, ValueError) as exc:
        raise PermissionError(f"{label} escaped MEDIA_ROOT") from exc


def _private_name(name: str) -> bool:
    return any(pattern.fullmatch(name) for pattern in _MARKER_PATTERNS) or name == ".reconcile.lock"


def _root_name_allowed(name: str, *, directory: bool) -> bool:
    if directory:
        return name in MANAGED_DIRECTORIES
    return name in {".backup.lock", ".reconcile.lock"}


def _file_name_allowed(name: str, *, root: bool) -> bool:
    if root:
        return _root_name_allowed(name, directory=False)
    return not name.startswith(".") or _private_name(name)


def _expected_file_mode(name: str, *, root: bool) -> int:
    if root and name == ".backup.lock":
        return BACKUP_LOCK_MODE
    return PRIVATE_MODE if _private_name(name) else MEDIA_MODE


def _set_or_check(
    fd: int,
    info: os.stat_result,
    *,
    uid: int,
    gid: int,
    mode: int,
    label: str,
    mutate: bool,
) -> None:
    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
        raise PermissionError(f"special files are not allowed in media: {label}")
    actual_mode = stat.S_IMODE(info.st_mode)
    if mutate:
        try:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
        except OSError as exc:
            raise PermissionError(f"could not normalize media entry {label}") from exc
    elif (info.st_uid, info.st_gid, actual_mode) != (uid, gid, mode):
        raise PermissionError(
            f"{label} has owner {info.st_uid}:{info.st_gid} mode {actual_mode:o}; "
            f"expected {uid}:{gid} mode {mode:o}"
        )


def _freeze_directory(fd: int, label: str) -> os.stat_result:
    """Make a directory root-owned and non-writable before reading its names."""
    info = _check_directory(fd, label)
    try:
        os.fchown(fd, 0, 0)
    except OSError as exc:
        if os.geteuid() != 0 and exc.errno in {errno.EACCES, errno.EPERM}:
            pass
        else:
            raise PermissionError(f"could not freeze media directory {label}") from exc
    try:
        os.fchmod(fd, 0o550)
    except OSError as exc:
        raise PermissionError(f"could not freeze media directory {label}") from exc
    return info


def _freeze_file(fd: int, label: str) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise PermissionError(f"special files are not allowed in media: {label}")
    try:
        os.fchown(fd, 0, 0)
    except OSError as exc:
        if os.geteuid() != 0 and exc.errno in {errno.EACCES, errno.EPERM}:
            pass
        else:
            raise PermissionError(f"could not freeze media file {label}") from exc
    try:
        os.fchmod(fd, PRIVATE_MODE)
    except OSError as exc:
        raise PermissionError(f"could not freeze media file {label}") from exc
    return info


def _freeze_directory_tree(
    fd: int,
    *,
    root_path: Path,
    root: bool,
    top_level: bool = False,
    seen: set[tuple[int, int]],
    files: list[_FileRecord],
    directories: list[_DirectoryRecord],
) -> None:
    before = _freeze_directory(fd, "MEDIA_ROOT" if root else "media directory")
    _fd_contained(fd, root_path, "MEDIA_ROOT" if root else "media directory")
    identity = (before.st_dev, before.st_ino)
    if identity in seen:
        raise PermissionError("media directory inode was visited twice")
    seen.add(identity)
    names: list[str] = []
    try:
        with os.scandir(fd) as entries:
            names = [entry.name for entry in entries]
    except OSError as exc:
        raise PermissionError("could not list media directory") from exc
    if root and not set(MANAGED_DIRECTORIES).issubset(names):
        raise PermissionError("MEDIA_ROOT is missing a managed directory")
    for name in names:
        child_info = _entry_lstat(fd, name)
        is_directory = stat.S_ISDIR(child_info.st_mode)
        if root and not _root_name_allowed(name, directory=is_directory):
            raise PermissionError(f"MEDIA_ROOT contains unmanaged root entry: {name}")
        if not is_directory and not _file_name_allowed(name, root=root):
            raise PermissionError(f"unknown operational file in media: {name}")
        child_fd = _open_entry(fd, name, directory=is_directory, before=child_info)
        keep_child = False
        try:
            after = os.fstat(child_fd)
            if is_directory:
                _fd_contained(child_fd, root_path, f"media directory {name}")
                _freeze_directory_tree(
                    child_fd,
                    root_path=root_path,
                    root=False,
                    top_level=root,
                    seen=seen,
                    files=files,
                    directories=directories,
                )
                keep_child = True
            else:
                frozen = _freeze_file(child_fd, name)
                files.append(
                    _FileRecord(
                        parent_fd=fd,
                        name=name,
                        identity=(frozen.st_dev, frozen.st_ino),
                        mode=_expected_file_mode(name, root=root),
                    )
                )
                if (after.st_dev, after.st_ino) != (frozen.st_dev, frozen.st_ino):
                    raise PermissionError(f"media entry changed identity: {name}")
        finally:
            if not keep_child:
                os.close(child_fd)
    directories.append(
        _DirectoryRecord(
            fd=fd,
            identity=identity,
            mode=ROOT_MODE if root or top_level else DIRECTORY_MODE,
        )
    )


def _walk_directory(
    fd: int,
    *,
    root_path: Path,
    uid: int,
    gid: int,
    root: bool,
    seen: set[tuple[int, int]],
    mutate: bool,
) -> None:
    """Read-only descriptor traversal used by the verifier."""
    info = _check_directory(fd, "MEDIA_ROOT" if root else "media directory")
    _fd_contained(fd, root_path, "MEDIA_ROOT" if root else "media directory")
    identity = (info.st_dev, info.st_ino)
    if identity in seen:
        raise PermissionError("media directory inode was visited twice")
    seen.add(identity)
    try:
        with os.scandir(fd) as entries:
            names = [entry.name for entry in entries]
    except OSError as exc:
        raise PermissionError("could not list media directory") from exc
    if root and not set(MANAGED_DIRECTORIES).issubset(names):
        raise PermissionError("MEDIA_ROOT is missing a managed directory")
    if root:
        _set_or_check(
            fd,
            info,
            uid=uid,
            gid=gid,
            mode=ROOT_MODE,
            label="MEDIA_ROOT",
            mutate=mutate,
        )
    for name in names:
        child_info = _entry_lstat(fd, name)
        is_directory = stat.S_ISDIR(child_info.st_mode)
        if root and not _root_name_allowed(name, directory=is_directory):
            raise PermissionError(f"MEDIA_ROOT contains unmanaged root entry: {name}")
        if not is_directory and not _file_name_allowed(name, root=root):
            raise PermissionError(f"unknown operational file in media: {name}")
        child_fd = _open_entry(fd, name, directory=is_directory, before=child_info)
        try:
            if is_directory:
                _set_or_check(
                    child_fd,
                    os.fstat(child_fd),
                    uid=uid,
                    gid=gid,
                    mode=ROOT_MODE if root and name in MANAGED_DIRECTORIES else DIRECTORY_MODE,
                    label=name,
                    mutate=mutate,
                )
                _walk_directory(
                    child_fd,
                    root_path=root_path,
                    uid=uid,
                    gid=gid,
                    root=False,
                    seen=seen,
                    mutate=mutate,
                )
            else:
                _set_or_check(
                    child_fd,
                    os.fstat(child_fd),
                    uid=uid,
                    gid=gid,
                    mode=_expected_file_mode(name, root=root),
                    label=name,
                    mutate=mutate,
                )
        finally:
            os.close(child_fd)


def _restore_frozen_tree(
    files: list[_FileRecord],
    directories: list[_DirectoryRecord],
    *,
    uid: int,
    gid: int,
    root_path: Path,
) -> None:
    # Every parent descriptor is still frozen, so reopening by dir_fd cannot
    # be redirected by the app user. Verify the frozen inode before restoring.
    for record in files:
        fd = _open_entry(record.parent_fd, record.name, directory=False)
        try:
            _fd_contained(record.parent_fd, root_path, "media parent directory")
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != record.identity or not stat.S_ISREG(info.st_mode):
                raise PermissionError(f"media entry changed identity: {record.name}")
            _set_or_check(
                fd,
                info,
                uid=uid,
                gid=gid,
                mode=record.mode,
                label=record.name,
                mutate=True,
            )
        finally:
            os.close(fd)
    for record in directories:
        _fd_contained(record.fd, root_path, "media directory")
        info = os.fstat(record.fd)
        if (info.st_dev, info.st_ino) != record.identity:
            raise PermissionError("media directory changed identity")
        _set_or_check(
            record.fd,
            info,
            uid=uid,
            gid=gid,
            mode=record.mode,
            label="media directory",
            mutate=True,
        )


def _fail_closed(root_fd: int, root_path: Path, seen: set[tuple[int, int]]) -> None:
    """Best-effort root ownership after a failed normalization."""
    try:
        info = _freeze_directory(root_fd, "MEDIA_ROOT")
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            return
        seen.add(identity)
        try:
            with os.scandir(root_fd) as entries:
                names = [entry.name for entry in entries]
        except OSError:
            return
        for name in names:
            try:
                child_info = os.lstat(name, dir_fd=root_fd)
                if stat.S_ISLNK(child_info.st_mode):
                    os.chown(name, 0, 0, dir_fd=root_fd, follow_symlinks=False)
                    continue
                if stat.S_ISDIR(child_info.st_mode):
                    child_fd = _open_entry(root_fd, name, directory=True, before=child_info)
                    try:
                        _fail_closed(child_fd, root_path, seen)
                    finally:
                        os.close(child_fd)
                    continue
                if stat.S_ISREG(child_info.st_mode):
                    child_fd = _open_entry(root_fd, name, directory=False, before=child_info)
                    try:
                        _freeze_file(child_fd, name)
                    finally:
                        os.close(child_fd)
                else:
                    os.chown(name, 0, 0, dir_fd=root_fd, follow_symlinks=False)
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        return


def _resolve_ids(owner: str, group: str) -> tuple[int, int]:
    try:
        uid = pwd.getpwnam(owner).pw_uid
        gid = grp.getgrnam(group).gr_gid
    except KeyError as exc:
        raise PermissionError("MEDIA_OWNER or MEDIA_GROUP does not resolve") from exc
    return uid, gid


def run(root: str, owner: str, group: str, *, mutate: bool, require_backup_lock: bool) -> None:
    uid, gid = _resolve_ids(owner, group)
    root_fd = _open_root(root)
    root_path = Path(root).absolute().resolve(strict=True)
    frozen_directories: list[_DirectoryRecord] = []
    frozen_files: list[_FileRecord] = []
    frozen = mutate and os.geteuid() == 0
    try:
        if frozen:
            _freeze_directory_tree(
                root_fd,
                root_path=root_path,
                root=True,
                seen=set(),
                files=frozen_files,
                directories=frozen_directories,
            )
            frozen = True
            _restore_frozen_tree(
                frozen_files,
                frozen_directories,
                uid=uid,
                gid=gid,
                root_path=root_path,
            )
        else:
            _walk_directory(
                root_fd,
                root_path=root_path,
                uid=uid,
                gid=gid,
                root=True,
                seen=set(),
                mutate=mutate,
            )
        if require_backup_lock:
            lock_fd = _open_entry(root_fd, ".backup.lock", directory=False)
            try:
                _set_or_check(
                    lock_fd,
                    os.fstat(lock_fd),
                    uid=uid,
                    gid=gid,
                    mode=BACKUP_LOCK_MODE,
                    label=".backup.lock",
                    mutate=False,
                )
            finally:
                os.close(lock_fd)
    except PermissionError as exc:
        if mutate and frozen:
            _fail_closed(root_fd, root_path, set())
            raise PermissionError(
                f"{exc}; MEDIA_ROOT was left root-owned and non-writable; fix the reported entry "
                "and rerun normalize-media-permissions"
            ) from exc
        raise
    finally:
        for record in frozen_directories:
            if record.fd != root_fd:
                try:
                    os.close(record.fd)
                except OSError:
                    pass
        os.close(root_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "normalize", "verify", "validate-parent"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--require-backup-lock", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            _prepare(args.root, args.owner, args.group)
        elif args.action == "validate-parent":
            parent_fd, leaf, _candidate = _open_trusted_parent(args.root)
            try:
                try:
                    info = os.lstat(leaf, dir_fd=parent_fd)
                except FileNotFoundError:
                    return 0
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise PermissionError("MEDIA_ROOT must be a real directory")
                target_fd = _open_entry(parent_fd, leaf, directory=True, before=info)
                os.close(target_fd)
            finally:
                os.close(parent_fd)
        else:
            run(
                args.root,
                args.owner,
                args.group,
                mutate=args.action == "normalize",
                require_backup_lock=args.require_backup_lock or args.action == "verify",
            )
    except PermissionError as exc:
        print(f"media-permissions: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
