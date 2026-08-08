"""Bounded, orientation-normalized image opening for the legacy renderer."""

from __future__ import annotations

import io
import os
from os import PathLike
from pathlib import Path
from collections.abc import Callable
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError

SUPPORTED_FORMATS = frozenset({"BMP", "JPEG", "PNG", "TIFF", "WEBP"})
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_PIXELS = 60_000_000
MAX_BYTES_ENV = "BEFORE_AFTER_IMAGE_MAX_BYTES"
MAX_PIXELS_ENV = "BEFORE_AFTER_IMAGE_MAX_PIXELS"
MAX_REQUEST_BYTES_ENV = "BEFORE_AFTER_IMAGE_MAX_REQUEST_BYTES"
REQUEST_OVERHEAD_BYTES = 64 * 1024


class ImagePolicyError(ValueError):
    """Raised when an image violates the renderer's input policy."""


def _configured_limit(value: int | None, env_name: str, default: int) -> int:
    raw = value if value is not None else os.environ.get(env_name, default)
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise ImagePolicyError(f"{env_name} must be a positive integer") from exc
    if limit <= 0:
        raise ImagePolicyError(f"{env_name} must be a positive integer")
    return limit


def configured_request_limit() -> int:
    """Return the Flask request cap for the configured image byte policy."""
    if os.environ.get(MAX_REQUEST_BYTES_ENV) is not None:
        return _configured_limit(None, MAX_REQUEST_BYTES_ENV, DEFAULT_MAX_BYTES + REQUEST_OVERHEAD_BYTES)
    return _configured_limit(None, MAX_BYTES_ENV, DEFAULT_MAX_BYTES) + REQUEST_OVERHEAD_BYTES


def _seekable_source_size(source: object) -> tuple[int, int] | None:
    seekable = getattr(source, "seekable", None)
    if callable(seekable):
        try:
            if not seekable():
                return None
        except (OSError, TypeError, ValueError):
            return None

    tell = getattr(source, "tell", None)
    seek = getattr(source, "seek", None)
    if tell is None or seek is None:
        return None
    try:
        position = int(tell())
        seek(0, io.SEEK_END)
        size = int(tell())
        seek(position)
        return position, size
    except (OSError, TypeError, ValueError):
        if "position" in locals():
            try:
                seek(position)
            except (OSError, TypeError, ValueError):
                pass
        return None


def _source_size(source: object) -> int | None:
    if isinstance(source, (str, PathLike)):
        return Path(source).stat().st_size
    if isinstance(source, (bytes, bytearray, memoryview)):
        return len(source)

    measured = _seekable_source_size(source)
    return measured[1] if measured is not None else None


def _read_bounded(source: BinaryIO, byte_limit: int) -> bytes:
    """Consume at most byte_limit + 1 bytes from an unknown-length stream."""
    chunks: list[bytes] = []
    total = 0
    while total <= byte_limit:
        size = min(64 * 1024, byte_limit + 1 - total)
        chunk = source.read(size)
        if chunk is None:
            raise ImagePolicyError("image stream returned no data")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ImagePolicyError("image stream must return bytes")
        chunk = bytes(chunk)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > byte_limit:
            raise ImagePolicyError(f"image exceeds byte limit ({total} > {byte_limit})")
    return b"".join(chunks)


def _stream(
    source: str | PathLike[str] | bytes | bytearray | memoryview | BinaryIO,
    byte_limit: int,
) -> tuple[BinaryIO, bool, Callable[[], None] | None]:
    if isinstance(source, (str, PathLike)):
        path = Path(source)
        size = path.stat().st_size
        if size > byte_limit:
            raise ImagePolicyError(f"image exceeds byte limit ({size} > {byte_limit})")
        return path.open("rb"), True, None
    if isinstance(source, (bytes, bytearray, memoryview)):
        payload = bytes(source)
        if len(payload) > byte_limit:
            raise ImagePolicyError(f"image exceeds byte limit ({len(payload)} > {byte_limit})")
        return io.BytesIO(payload), True, None
    if not hasattr(source, "read"):
        raise TypeError("image source must be a path, bytes, or binary stream")

    measured = _seekable_source_size(source)
    if measured is None:
        # Inherently non-seekable streams are consumed; the owned buffer is
        # what Pillow reads, and the caller-owned stream is never closed.
        return io.BytesIO(_read_bounded(source, byte_limit)), True, None

    position, size = measured
    if size > byte_limit:
        raise ImagePolicyError(f"image exceeds byte limit ({size} > {byte_limit})")
    try:
        source.seek(0)
    except (OSError, TypeError, ValueError):
        return io.BytesIO(_read_bounded(source, byte_limit)), True, None

    def restore() -> None:
        try:
            source.seek(position)
        except (OSError, TypeError, ValueError):
            pass

    return source, False, restore


def open_image(
    source: str | PathLike[str] | bytes | bytearray | memoryview | BinaryIO,
    *,
    max_bytes: int | None = None,
    max_pixels: int | None = None,
) -> Image.Image:
    """Open one supported, non-animated image detached from its source.

    Limits default to environment-configured values and can be overridden by
    callers for tests or a narrower workflow. Pillow's own bomb protection is
    intentionally left unchanged.
    """
    byte_limit = _configured_limit(max_bytes, MAX_BYTES_ENV, DEFAULT_MAX_BYTES)
    pixel_limit = _configured_limit(max_pixels, MAX_PIXELS_ENV, DEFAULT_MAX_PIXELS)
    stream, owned, restore = _stream(source, byte_limit)
    try:
        try:
            with Image.open(stream) as image:
                image_format = (image.format or "").upper()
                if image_format not in SUPPORTED_FORMATS:
                    raise ImagePolicyError(f"unsupported image format: {image_format or 'unknown'}")
                if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
                    raise ImagePolicyError("animated images are not supported")

                # Check the header dimensions before EXIF transpose can decode pixels.
                width, height = image.size
                pixels = width * height
                if pixels > pixel_limit:
                    raise ImagePolicyError(f"image exceeds pixel limit ({pixels} > {pixel_limit})")

                oriented = ImageOps.exif_transpose(image)
                try:
                    oriented.load()
                    result = oriented.copy()
                finally:
                    if oriented is not image:
                        oriented.close()
                result.format = image_format
                return result
        except ImagePolicyError:
            raise
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise ImagePolicyError(f"could not decode image: {exc}") from exc
    finally:
        if owned:
            stream.close()
        if restore is not None:
            restore()
