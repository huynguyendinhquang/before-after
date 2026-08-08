"""Bounded, orientation-normalized image opening for the legacy renderer."""

from __future__ import annotations

import io
import os
from os import PathLike
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError

SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "TIFF", "WEBP"})
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_PIXELS = 60_000_000
MAX_BYTES_ENV = "BEFORE_AFTER_IMAGE_MAX_BYTES"
MAX_PIXELS_ENV = "BEFORE_AFTER_IMAGE_MAX_PIXELS"


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


def _source_size(source: object) -> int | None:
    if isinstance(source, (str, PathLike)):
        return Path(source).stat().st_size
    if isinstance(source, (bytes, bytearray, memoryview)):
        return len(source)

    declared = getattr(source, "content_length", None)
    if declared is not None:
        try:
            return int(declared)
        except (TypeError, ValueError):
            pass

    tell = getattr(source, "tell", None)
    seek = getattr(source, "seek", None)
    if tell is None or seek is None:
        return None
    try:
        position = tell()
        seek(0, io.SEEK_END)
        size = tell()
        seek(position)
        return int(size)
    except (OSError, TypeError, ValueError):
        return None


def _stream(source: str | PathLike[str] | bytes | bytearray | memoryview | BinaryIO) -> tuple[BinaryIO, bool]:
    if isinstance(source, (str, PathLike)):
        return Path(source).open("rb"), True
    if isinstance(source, (bytes, bytearray, memoryview)):
        return io.BytesIO(bytes(source)), True
    if not hasattr(source, "read"):
        raise TypeError("image source must be a path, bytes, or binary stream")
    try:
        source.seek(0)
    except (AttributeError, OSError, TypeError):
        pass
    return source, False


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
    size = _source_size(source)
    if size is not None and size > byte_limit:
        raise ImagePolicyError(f"image exceeds byte limit ({size} > {byte_limit})")

    stream, owned = _stream(source)
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
