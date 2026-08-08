"""Managed filesystem storage for immutable originals and preview derivatives."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import io
import math
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from PIL import ExifTags, Image

from app.image_policy import FORMAT_EXTENSIONS, ImagePolicyError, open_image


PREVIEW_MAX_DIMENSION = 1600
DEFAULT_ORPHAN_GRACE_SECONDS = 300
_ALLOWED_ROOTS = frozenset({"originals", "previews", "quarantine"})
_HEX_KEY = re.compile(r"^[0-9a-f]{32}$")
_PENDING_PREFIX = ".pending-"
_PENDING_MARKER_NAME = re.compile(r"^\.pending-([0-9a-f]{32})$")
_TEMP_NAME = re.compile(r"^\.upload-.+\.tmp$")
_POSIX_DIRFD = (
    os.name == "posix"
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.link in os.supports_dir_fd
)


class StorageError(ValueError):
    """Raised for invalid media or an unsafe managed-storage operation."""


@dataclass(frozen=True)
class ImageInspection:
    format: str
    width: int
    height: int
    byte_count: int
    sha256: str
    suggested_capture_date: date | None
    preview_bytes: bytes


@dataclass(frozen=True)
class StoredMedia:
    original_key: str
    preview_key: str
    pending_key: str | None = None


class ManagedStorage:
    """Own media paths and make every write atomic and independently cleanable."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if not _POSIX_DIRFD:
            raise StorageError("managed storage requires POSIX dirfd primitives")
        self.root = Path(root).expanduser().absolute()
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._root_fd = os.open(self.root, self._directory_flags())
        except (OSError, TypeError, ValueError) as exc:
            raise StorageError(f"MEDIA_ROOT is not usable: {exc}") from exc

        try:
            self._validate_directory(self._root_fd, "MEDIA_ROOT")
            for name in _ALLOWED_ROOTS:
                self._ensure_directory(self._root_fd, name)
        except Exception:
            fd = self._root_fd
            self._root_fd = None
            os.close(fd)
            raise
        self._pending_fds: dict[str, int] = {}
        self._reconciliation_state = threading.local()

    def __del__(self) -> None:
        for fd in getattr(self, "_pending_fds", {}).values():
            try:
                os.close(fd)
            except OSError:
                pass
        getattr(self, "_pending_fds", {}).clear()
        fd = getattr(self, "_root_fd", None)
        if fd is not None:
            self._root_fd = None
            try:
                os.close(fd)
            except OSError:
                pass

    @staticmethod
    def _directory_flags() -> int:
        return (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )

    @staticmethod
    def _read_flags() -> int:
        return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    @staticmethod
    def _validate_directory(fd: int, label: str) -> None:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise StorageError(f"could not inspect {label}: {exc}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise StorageError(f"{label} must be a directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise StorageError(f"{label} must be private to the application user")

    def _ensure_directory(self, parent_fd: int, name: str) -> None:
        created = False
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        try:
            fd = os.open(name, self._directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise StorageError(f"managed storage directory is unsafe: {name}") from exc
        try:
            self._validate_directory(fd, name)
        finally:
            os.close(fd)
        if created:
            self._fsync_directory(parent_fd)

    def _key_parts(self, key: str) -> tuple[str, ...]:
        if not isinstance(key, str) or not key or "\x00" in key or "\\" in key:
            raise StorageError("invalid storage key")
        parts = tuple(key.split("/"))
        if (
            not parts
            or parts[0] not in _ALLOWED_ROOTS
            or any(not part or part in {".", ".."} for part in parts)
        ):
            raise StorageError("invalid storage key")
        return parts

    def _open_parent(self, parts: tuple[str, ...]) -> int:
        fd = os.dup(self._root_fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, self._directory_flags(), dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except FileNotFoundError as exc:
            os.close(fd)
            raise StorageError("managed storage directory is missing") from exc
        except OSError as exc:
            os.close(fd)
            raise StorageError("symlink or invalid managed storage directory") from exc

    def _fsync_directory(self, fd: int) -> None:
        try:
            os.fsync(fd)
        except OSError as exc:
            raise StorageError(f"could not fsync managed storage directory: {exc}") from exc

    def _open_reconciliation_lock(self) -> int:
        fd: int | None = None
        try:
            fd = os.open(
                ".reconcile.lock",
                os.O_RDWR
                | os.O_CREAT
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=self._root_fd,
            )
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise StorageError("reconciliation lock is unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fd
        except StorageError:
            if fd is not None:
                os.close(fd)
            raise
        except (OSError, TypeError, ValueError) as exc:
            if fd is not None:
                os.close(fd)
            raise StorageError(f"could not acquire reconciliation lock: {exc}") from exc

    @contextmanager
    def reconciliation_lock(self):
        """Exclusively guard a reference snapshot and managed-media cleanup."""
        state = self._reconciliation_state
        if getattr(state, "fd", None) is not None:
            state.depth += 1
            try:
                yield
            finally:
                state.depth -= 1
            return

        fd = self._open_reconciliation_lock()
        state.fd = fd
        state.depth = 1
        try:
            yield
        finally:
            state.fd = None
            state.depth = 0
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _create_pending_marker(
        self,
        key: str,
        original_key: str,
        preview_key: str,
    ) -> None:
        parts = self._key_parts(key)
        parent_fd: int | None = None
        temporary_name: str | None = None
        fd: int | None = None
        published = False
        success = False
        try:
            parent_fd = self._open_parent(parts)
            try:
                existing_fd = os.open(parts[-1], self._read_flags(), dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            else:
                os.close(existing_fd)
                raise StorageError("pending marker collision")

            temporary_name = f"{parts[-1]}.tmp"
            fd = os.open(
                temporary_name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            payload = f"{original_key}\n{preview_key}\n".encode("ascii")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("could not write pending marker")
                view = view[written:]
            os.fsync(fd)
            os.replace(
                temporary_name,
                parts[-1],
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_name = None
            published = True
            self._fsync_directory(parent_fd)
            self._pending_fds[key] = fd
            fd = None
            success = True
        except FileExistsError as exc:
            raise StorageError("pending marker collision") from exc
        except StorageError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise StorageError(f"could not create pending marker: {exc}") from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if not success and parent_fd is not None:
                if published:
                    temporary_name = parts[-1]
                if temporary_name is not None:
                    try:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                    except (FileNotFoundError, OSError):
                        pass
                try:
                    self._fsync_directory(parent_fd)
                except StorageError:
                    pass
            if parent_fd is not None:
                os.close(parent_fd)

    def _release_pending(self, key: str | None) -> None:
        if not key:
            return
        fd = self._pending_fds.pop(key, None)
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def finalize(self, stored: StoredMedia) -> None:
        """Release a committed store's marker without touching its media."""
        with self.reconciliation_lock():
            try:
                self.cleanup(stored.pending_key)
            finally:
                self._release_pending(stored.pending_key)

    def discard(self, stored: StoredMedia) -> None:
        """Release a failed store and best-effort remove its media and marker."""
        with self.reconciliation_lock():
            try:
                for key in (stored.original_key, stored.preview_key, stored.pending_key):
                    try:
                        self.cleanup(key)
                    except StorageError:
                        pass
            finally:
                self._release_pending(stored.pending_key)

    def _atomic_write(self, key: str, payload: bytes) -> None:
        parts = self._key_parts(key)
        parent_fd: int | None = None
        temporary_name: str | None = None
        fd: int | None = None
        linked = False
        success = False
        try:
            parent_fd = self._open_parent(parts)
            for _ in range(8):
                candidate = f".upload-{uuid.uuid4().hex}.tmp"
                try:
                    fd = os.open(
                        candidate,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=parent_fd,
                    )
                    temporary_name = candidate
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except FileExistsError:
                    continue
            if fd is None or temporary_name is None:
                raise StorageError("could not create a unique temporary media file")

            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

            os.link(
                temporary_name,
                parts[-1],
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            linked = True
            os.unlink(temporary_name, dir_fd=parent_fd)
            temporary_name = None
            self._fsync_directory(parent_fd)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            fd = None
            success = True
        except FileExistsError as exc:
            raise StorageError("storage key collision") from exc
        except StorageError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise StorageError(f"could not store media: {exc}") from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if parent_fd is not None:
                if temporary_name is not None:
                    try:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                if linked and not success:
                    try:
                        os.unlink(parts[-1], dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                os.close(parent_fd)

    def resolve(self, key: str) -> Path:
        """Return an existing regular managed path after dirfd checks."""
        parts = self._key_parts(key)
        parent_fd = self._open_parent(parts)
        try:
            try:
                fd = os.open(parts[-1], self._read_flags(), dir_fd=parent_fd)
            except FileNotFoundError as exc:
                raise StorageError("media is missing") from exc
            except OSError as exc:
                raise StorageError("media path is outside MEDIA_ROOT") from exc
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise StorageError("managed media is not a regular file")
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)
        return self.root.joinpath(*parts)

    def open_read(self, key: str):
        """Open managed media without following any managed-path symlink."""
        parts = self._key_parts(key)
        parent_fd = self._open_parent(parts)
        try:
            try:
                fd = os.open(parts[-1], self._read_flags(), dir_fd=parent_fd)
            except FileNotFoundError as exc:
                raise StorageError("media is missing") from exc
            except OSError as exc:
                raise StorageError(f"could not open media: {exc}") from exc
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise StorageError("managed media is not a regular file")
                return os.fdopen(fd, "rb")
            except Exception:
                os.close(fd)
                raise
        finally:
            os.close(parent_fd)

    def cleanup(self, *keys: str | None) -> None:
        """Remove only managed regular files owned by a failed operation."""
        for key in keys:
            if not key:
                continue
            parts = self._key_parts(key)
            parent_fd = self._open_parent(parts)
            try:
                try:
                    info = os.lstat(parts[-1], dir_fd=parent_fd)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    raise StorageError("symlink in managed storage path")
                if not stat.S_ISREG(info.st_mode):
                    raise StorageError("refusing to remove non-file media")
                try:
                    os.unlink(parts[-1], dir_fd=parent_fd)
                except FileNotFoundError:
                    continue
                self._fsync_directory(parent_fd)
            finally:
                os.close(parent_fd)

    def _regular_entries(self, directory: str) -> dict[str, float]:
        directory_fd = self._open_parent((directory, ".scan"))
        entries: dict[str, float] = {}
        try:
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(info.st_mode):
                        entries[entry.name] = info.st_mtime
        finally:
            os.close(directory_fd)
        return entries

    def _try_locked_file(self, key: str) -> tuple[int, float] | None:
        parts = self._key_parts(key)
        parent_fd: int | None = None
        fd: int | None = None
        locked = False
        try:
            parent_fd = self._open_parent(parts)
            fd = os.open(parts[-1], self._read_flags(), dir_fd=parent_fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None
            locked = True
            return fd, info.st_mtime
        except FileNotFoundError:
            return None
        except BlockingIOError:
            return None
        except (OSError, StorageError) as exc:
            raise StorageError(f"could not lock pending marker: {exc}") from exc
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
            if fd is not None and not locked:
                os.close(fd)

    def _pending_media_keys(self, marker_key: str) -> tuple[str, str]:
        marker_parts = self._key_parts(marker_key)
        marker_match = (
            _PENDING_MARKER_NAME.fullmatch(marker_parts[-1])
            if len(marker_parts) == 2 and marker_parts[0] == "quarantine"
            else None
        )
        if marker_match is None:
            raise StorageError("pending marker name is invalid")
        marker_stem = marker_match.group(1)
        try:
            with self.open_read(marker_key) as marker:
                payload = marker.read(2048)
                if marker.read(1):
                    raise StorageError("pending marker is too large")
            lines = payload.decode("ascii").split("\n")
            if len(lines) != 3 or lines[-1] != "":
                raise StorageError("pending marker is incomplete")
            original_key, preview_key = lines[:2]
            original_parts = self._key_parts(original_key)
            if len(original_parts) != 2 or original_parts[0] != "originals":
                raise StorageError("pending marker original key is invalid")
            original_name = original_parts[-1]
            original_stem = Path(original_name).stem
            extension = Path(original_name).suffix.lower().lstrip(".")
            if original_stem != marker_stem or not _HEX_KEY.fullmatch(original_stem):
                raise StorageError("pending marker stem does not match its key")
            if extension not in FORMAT_EXTENSIONS.values():
                raise StorageError("pending marker original extension is invalid")
            if ManagedStorage.preview_key(original_key) != preview_key:
                raise StorageError("pending marker does not match its media")
            preview_parts = self._key_parts(preview_key)
            if (
                len(preview_parts) != 2
                or preview_parts[0] != "previews"
                or preview_parts[-1] != f"{marker_stem}.jpg"
            ):
                raise StorageError("pending marker preview key is invalid")
        except (UnicodeError, ValueError) as exc:
            raise StorageError("pending marker is invalid") from exc
        return original_key, preview_key

    def reconcile(
        self,
        referenced_keys: Iterable[str],
        *,
        grace_seconds: float = DEFAULT_ORPHAN_GRACE_SECONDS,
        now: float | None = None,
    ) -> list[str]:
        with self.reconciliation_lock():
            return self._reconcile(
                referenced_keys,
                grace_seconds=grace_seconds,
                now=now,
            )

    def _reconcile(
        self,
        referenced_keys: Iterable[str],
        *,
        grace_seconds: float = DEFAULT_ORPHAN_GRACE_SECONDS,
        now: float | None = None,
    ) -> list[str]:
        """Remove stale unreferenced media left by a crashed store."""
        try:
            grace = float(grace_seconds)
            timestamp = time.time() if now is None else float(now)
        except (OverflowError, TypeError, ValueError) as exc:
            raise StorageError("orphan grace period and timestamp must be finite") from exc
        if not math.isfinite(grace) or grace < 0 or not math.isfinite(timestamp):
            raise StorageError("orphan grace period and timestamp must be finite")

        referenced = {key for key in referenced_keys if isinstance(key, str)}
        referenced_previews: set[str] = set()
        for key in referenced:
            try:
                referenced_previews.add(self.preview_key(key))
            except StorageError:
                continue

        def is_referenced(key: str) -> bool:
            return key in referenced or key in referenced_previews

        def is_stale(modified: float) -> bool:
            return timestamp - modified >= grace

        def remove(key: str) -> bool:
            try:
                self.cleanup(key)
            except StorageError:
                return False
            removed.append(key)
            return True

        originals = self._regular_entries("originals")
        previews = self._regular_entries("previews")
        quarantine = self._regular_entries("quarantine")
        protected_stems: set[str] = set()
        removed: list[str] = []

        for directory, entries in (("originals", originals), ("previews", previews)):
            for name, modified in entries.items():
                if not _TEMP_NAME.fullmatch(name):
                    continue
                locked = self._try_locked_file(f"{directory}/{name}")
                if locked is None:
                    continue
                temp_fd, modified = locked
                try:
                    if not is_stale(modified):
                        continue
                    remove(f"{directory}/{name}")
                finally:
                    try:
                        fcntl.flock(temp_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(temp_fd)

        for marker_name, marker_modified in quarantine.items():
            if not marker_name.startswith(_PENDING_PREFIX):
                continue
            marker_key = f"quarantine/{marker_name}"
            marker_match = _PENDING_MARKER_NAME.fullmatch(marker_name)
            locked = self._try_locked_file(marker_key)
            if locked is None:
                if marker_match is not None:
                    protected_stems.add(marker_match.group(1))
                continue
            marker_fd, marker_modified = locked
            try:
                marker_stale = is_stale(marker_modified)
                if marker_match is not None and not marker_stale:
                    protected_stems.add(marker_match.group(1))
                try:
                    original_key, preview_key = self._pending_media_keys(marker_key)
                except StorageError:
                    if marker_stale:
                        remove(marker_key)
                    continue
                referenced_media = (
                    is_referenced(original_key) or is_referenced(preview_key)
                )
                if referenced_media:
                    try:
                        self.cleanup(marker_key)
                    except StorageError:
                        pass
                    continue
                if not marker_stale:
                    protected_stems.add(marker_match.group(1))
                    continue
                original_name = Path(original_key).name
                preview_name = Path(preview_key).name
                if original_key not in referenced and original_name in originals:
                    if is_stale(originals[original_name]):
                        remove(original_key)
                if preview_key not in referenced and preview_name in previews:
                    if is_stale(previews[preview_name]):
                        remove(preview_key)
                remove(marker_key)
            finally:
                try:
                    fcntl.flock(marker_fd, fcntl.LOCK_UN)
                finally:
                    os.close(marker_fd)

        original_by_stem = {
            Path(name).stem: name
            for name in originals
            if _HEX_KEY.fullmatch(Path(name).stem)
        }
        preview_by_stem = {
            Path(name).stem: name
            for name in previews
            if _HEX_KEY.fullmatch(Path(name).stem)
        }
        stems = set(original_by_stem) | set(preview_by_stem)
        for stem in sorted(stems - protected_stems):
            original_name = original_by_stem.get(stem)
            preview_name = preview_by_stem.get(stem)
            original_key = f"originals/{original_name}" if original_name else None
            preview_key = f"previews/{preview_name}" if preview_name else None
            for key, modified in (
                (original_key, originals[original_name]) if original_name else (None, 0),
                (preview_key, previews[preview_name]) if preview_name else (None, 0),
            ):
                if key is None or is_referenced(key) or not is_stale(modified):
                    continue
                remove(key)
        return removed

    def quarantine(self, key: str) -> str | None:
        """Move an existing managed file to a generated quarantine key."""
        source_parts = self._key_parts(key)
        source_fd = self._open_parent(source_parts)
        target_key: str | None = None
        target_parts: tuple[str, ...] | None = None
        target_fd: int | None = None
        linked = False
        source_unlinked = False
        success = False
        try:
            try:
                info = os.lstat(source_parts[-1], dir_fd=source_fd)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise StorageError("refusing to quarantine non-file media")

            suffix = Path(source_parts[-1]).suffix.lower() or ".bin"
            target_key = f"quarantine/{uuid.uuid4().hex}{suffix}"
            target_parts = self._key_parts(target_key)
            target_fd = self._open_parent(target_parts)
            os.link(
                source_parts[-1],
                target_parts[-1],
                src_dir_fd=source_fd,
                dst_dir_fd=target_fd,
                follow_symlinks=False,
            )
            linked = True
            self._fsync_directory(target_fd)
            os.unlink(source_parts[-1], dir_fd=source_fd)
            source_unlinked = True
            self._fsync_directory(source_fd)
            success = True
            return target_key
        except StorageError:
            raise
        except (OSError, ValueError) as exc:
            raise StorageError(f"could not quarantine media: {exc}") from exc
        finally:
            if (
                linked
                and not success
                and not source_unlinked
                and target_fd is not None
                and target_parts is not None
            ):
                try:
                    os.unlink(target_parts[-1], dir_fd=target_fd)
                except (FileNotFoundError, OSError):
                    pass
            if target_fd is not None:
                os.close(target_fd)
            os.close(source_fd)

    @staticmethod
    def preview_key(original_key: str) -> str:
        relative = PurePosixPath(original_key)
        if len(relative.parts) != 2 or relative.parts[0] != "originals":
            raise StorageError("invalid original storage key")
        stem = Path(relative.parts[1]).stem
        if not _HEX_KEY.fullmatch(stem):
            raise StorageError("invalid original storage key")
        return f"previews/{stem}.jpg"

    @staticmethod
    def _suggested_date(image: Image.Image) -> date | None:
        try:
            exif = image.getexif()
            values = [exif.get(36867)]
            try:
                values.append(exif.get_ifd(ExifTags.IFD.Exif).get(36867))
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
        except (AttributeError, OSError, TypeError, ValueError):
            return None
        for value in values:
            if isinstance(value, bytes):
                value = value.decode("ascii", "ignore")
            if not isinstance(value, str):
                continue
            match = re.match(r"^\s*(\d{4}):(\d{2}):(\d{2})", value)
            if not match:
                continue
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
        return None

    @staticmethod
    def _preview_bytes(image: Image.Image) -> bytes:
        preview = image.convert("RGB")
        clean: Image.Image | None = None
        try:
            preview.thumbnail(
                (PREVIEW_MAX_DIMENSION, PREVIEW_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            clean = Image.new("RGB", preview.size)
            clean.paste(preview)
            output = io.BytesIO()
            clean.save(output, format="JPEG", quality=85, optimize=True)
            return output.getvalue()
        finally:
            if clean is not None:
                clean.close()
            preview.close()

    def inspect(self, payload: bytes | bytearray | memoryview) -> ImageInspection:
        try:
            raw = bytes(payload)
        except (TypeError, ValueError) as exc:
            raise StorageError("image payload must contain bytes") from exc
        if not raw:
            raise StorageError("image payload is empty")
        digest = hashlib.sha256(raw).hexdigest()
        try:
            with open_image(raw) as image:
                image_format = (image.format or "").upper()
                if image_format not in FORMAT_EXTENSIONS:
                    raise StorageError("unsupported image format")
                return ImageInspection(
                    format=image_format,
                    width=image.width,
                    height=image.height,
                    byte_count=len(raw),
                    sha256=digest,
                    suggested_capture_date=self._suggested_date(image),
                    preview_bytes=self._preview_bytes(image),
                )
        except StorageError:
            raise
        except ImagePolicyError as exc:
            raise StorageError(str(exc)) from exc

    def validate(self, payload: bytes | bytearray | memoryview) -> ImageInspection:
        """Validate one payload and return its bounded image inspection."""
        return self.inspect(payload)

    def preview(self, payload: bytes | bytearray | memoryview) -> bytes:
        """Return a metadata-free bounded preview for one validated payload."""
        return self.inspect(payload).preview_bytes

    def store(self, payload: bytes, inspection: ImageInspection | None = None) -> StoredMedia:
        if inspection is None:
            inspection = self.inspect(payload)
        raw = bytes(payload)
        if hashlib.sha256(raw).hexdigest() != inspection.sha256:
            raise StorageError("image inspection does not match payload")
        extension = FORMAT_EXTENSIONS.get(inspection.format)
        if extension is None:
            raise StorageError("unsupported image format")
        stem = uuid.uuid4().hex
        original_key = f"originals/{stem}.{extension}"
        preview_key = f"previews/{stem}.jpg"
        pending_key = f"quarantine/{_PENDING_PREFIX}{stem}"
        written: list[str] = []
        with self.reconciliation_lock():
            try:
                self._create_pending_marker(pending_key, original_key, preview_key)
                self._atomic_write(original_key, raw)
                written.append(original_key)
                self._atomic_write(preview_key, inspection.preview_bytes)
                written.append(preview_key)
                return StoredMedia(
                    original_key=original_key,
                    preview_key=preview_key,
                    pending_key=pending_key,
                )
            except Exception:
                for key in (*reversed(written), pending_key):
                    try:
                        self.cleanup(key)
                    except StorageError:
                        pass
                self._release_pending(pending_key)
                raise
