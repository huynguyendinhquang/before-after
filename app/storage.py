"""Managed filesystem storage for immutable originals and preview derivatives."""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from PIL import ExifTags, Image

from app.image_policy import FORMAT_EXTENSIONS, ImagePolicyError, open_image


PREVIEW_MAX_DIMENSION = 1600
_ALLOWED_ROOTS = frozenset({"originals", "previews", "quarantine"})
_HEX_KEY = re.compile(r"^[0-9a-f]{32}$")
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

    def __del__(self) -> None:
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
                    break
                except FileExistsError:
                    continue
            if fd is None or temporary_name is None:
                raise StorageError("could not create a unique temporary media file")

            with os.fdopen(fd, "wb") as stream:
                fd = None
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

    def quarantine(self, key: str) -> str | None:
        """Move an existing managed file to a generated quarantine key."""
        source_parts = self._key_parts(key)
        source_fd = self._open_parent(source_parts)
        target_key: str | None = None
        target_parts: tuple[str, ...] | None = None
        target_fd: int | None = None
        linked = False
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
            os.unlink(source_parts[-1], dir_fd=source_fd)
            self._fsync_directory(source_fd)
            self._fsync_directory(target_fd)
            success = True
            return target_key
        except StorageError:
            raise
        except (OSError, ValueError) as exc:
            raise StorageError(f"could not quarantine media: {exc}") from exc
        finally:
            if linked and not success and target_fd is not None and target_parts is not None:
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
        written: list[str] = []
        try:
            self._atomic_write(original_key, raw)
            written.append(original_key)
            self._atomic_write(preview_key, inspection.preview_bytes)
            written.append(preview_key)
            return StoredMedia(original_key=original_key, preview_key=preview_key)
        except Exception:
            self.cleanup(*written)
            raise
