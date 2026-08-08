"""Managed filesystem storage for immutable originals and preview derivatives."""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from PIL import ExifTags, Image

from app.image_policy import ImagePolicyError, open_image


FORMAT_EXTENSIONS = {
    "BMP": "bmp",
    "JPEG": "jpg",
    "PNG": "png",
    "TIFF": "tif",
    "WEBP": "webp",
}
FORMAT_MIMETYPES = {
    "BMP": "image/bmp",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "TIFF": "image/tiff",
    "WEBP": "image/webp",
}
PREVIEW_MAX_DIMENSION = 1600
_ALLOWED_ROOTS = frozenset({"originals", "previews", "quarantine"})
_HEX_KEY = re.compile(r"^[0-9a-f]{32}$")


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
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise StorageError("MEDIA_ROOT must be a directory")
        for name in _ALLOWED_ROOTS:
            directory = self.root / name
            try:
                directory.mkdir(exist_ok=True)
                mode = os.lstat(directory).st_mode
            except OSError as exc:
                raise StorageError(f"managed storage directory is unsafe: {name}") from exc
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise StorageError(f"managed storage directory is unsafe: {name}")

    def _safe_path(self, key: str) -> Path:
        if not isinstance(key, str) or not key or "\x00" in key or "\\" in key:
            raise StorageError("invalid storage key")
        relative = PurePosixPath(key)
        parts = relative.parts
        if (
            relative.is_absolute()
            or not parts
            or parts[0] not in _ALLOWED_ROOTS
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise StorageError("invalid storage key")
        path = self.root.joinpath(*parts)
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise StorageError("invalid storage key") from exc
        self._assert_no_symlinks(path)
        return path

    def _assert_no_symlinks(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise StorageError("path is outside MEDIA_ROOT") from exc
        current = self.root
        for part in relative.parts:
            current /= part
            try:
                mode = os.lstat(current).st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StorageError(f"could not inspect storage path: {exc}") from exc
            if stat.S_ISLNK(mode):
                raise StorageError("symlink in managed storage path")

    def _fsync_directory(self, directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _atomic_write(self, key: str, payload: bytes) -> None:
        path = self._safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlinks(path)
        temporary_name: str | None = None
        fd: int | None = None
        linked = False
        success = False
        try:
            fd, temporary_name = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=path.parent)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # A fully-written temporary inode is linked into place without
            # replacing an existing key, so readers never see partial bytes.
            os.link(temporary_name, path, follow_symlinks=False)
            linked = True
            os.unlink(temporary_name)
            temporary_name = None
            self._fsync_directory(path.parent)
            success = True
        except FileExistsError as exc:
            raise StorageError("storage key collision") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise StorageError(f"could not store media: {exc}") from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
            if linked and not success:
                try:
                    path.unlink()
                except OSError:
                    pass

    def resolve(self, key: str) -> Path:
        """Return an existing regular managed path after containment checks."""
        path = self._safe_path(key)
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError as exc:
            raise StorageError("media is missing") from exc
        except OSError as exc:
            raise StorageError(f"could not resolve media: {exc}") from exc
        if not stat.S_ISREG(mode):
            raise StorageError("managed media is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise StorageError("managed media is not a regular file")
            finally:
                os.close(fd)
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError("media path is outside MEDIA_ROOT") from exc
        return path

    def open_read(self, key: str):
        """Open managed media without following a final symlink."""
        path = self._safe_path(key)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode):
                os.close(fd)
                raise StorageError("managed media is not a regular file")
            return os.fdopen(fd, "rb")
        except StorageError:
            raise
        except FileNotFoundError as exc:
            raise StorageError("media is missing") from exc
        except OSError as exc:
            raise StorageError(f"could not open media: {exc}") from exc

    def cleanup(self, *keys: str | None) -> None:
        """Remove only managed files owned by a failed operation."""
        for key in keys:
            if not key:
                continue
            path = self._safe_path(key)
            try:
                mode = os.lstat(path).st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(mode):
                raise StorageError("refusing to remove a managed directory")
            try:
                path.unlink()
            except FileNotFoundError:
                continue

    def quarantine(self, key: str) -> str | None:
        """Move an existing managed file to a generated quarantine key."""
        source = self._safe_path(key)
        try:
            mode = os.lstat(source).st_mode
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(mode):
            raise StorageError("refusing to quarantine non-file media")
        suffix = Path(source.name).suffix.lower() or ".bin"
        target_key = f"quarantine/{uuid.uuid4().hex}{suffix}"
        target = self._safe_path(target_key)
        try:
            os.link(source, target, follow_symlinks=False)
            source.unlink()
            self._fsync_directory(source.parent)
            self._fsync_directory(target.parent)
        except (OSError, ValueError) as exc:
            self.cleanup(target_key)
            raise StorageError(f"could not quarantine media: {exc}") from exc
        return target_key

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
        try:
            preview.thumbnail(
                (PREVIEW_MAX_DIMENSION, PREVIEW_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            output = io.BytesIO()
            preview.save(output, format="JPEG", quality=85, optimize=True, exif=b"")
            return output.getvalue()
        finally:
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


def mimetype_for_format(image_format: str) -> str:
    return FORMAT_MIMETYPES.get(image_format.upper(), "application/octet-stream")
