"""Render a persistence-independent Comparison Set Canvas."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

from app.image_policy import open_image

MAX_TITLE_CHARS = 500
MAX_RENDER_DPI = 600
MAX_CANVAS_PIXELS = 100_000_000


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_color(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        ImageColor.getrgb(value)
    except (TypeError, ValueError):
        return False
    return True


def _font(size: int) -> ImageFont.ImageFont:
    for name in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ):
        if Path(name).exists():
            return ImageFont.truetype(name, size=size)
    return ImageFont.load_default()


def _font_bold(size: int) -> ImageFont.ImageFont:
    for name in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ):
        if Path(name).exists():
            return ImageFont.truetype(name, size=size)
    return _font(size)


# Persisted Canvas renderer ------------------------------------------------
#
# These objects intentionally contain no ORM or Flask types. The comparison
# workflow turns its rows into this small render specification before calling
# the functions below.

CANVAS_PRESETS: dict[str, tuple[float, float]] = {
    "16:9": (297.0, 167.06),
    "16:10": (297.0, 185.63),
    "a4-landscape": (297.0, 210.0),
    "a4-portrait": (210.0, 297.0),
}
_CANVAS_PRESET_ALIASES = {
    "16-9": "16:9",
    "16_9": "16:9",
    "16x9": "16:9",
    "16-10": "16:10",
    "16_10": "16:10",
    "16x10": "16:10",
    "a4_landscape": "a4-landscape",
    "a4 landscape": "a4-landscape",
    "a4_portrait": "a4-portrait",
    "a4 portrait": "a4-portrait",
    "custom-mm": "custom",
}


def normalize_canvas_preset(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Canvas preset is invalid")
    key = value.strip().casefold()
    key = _CANVAS_PRESET_ALIASES.get(key, key)
    if key not in {*CANVAS_PRESETS, "custom"}:
        raise ValueError("Canvas preset is invalid")
    return key


def canvas_dimensions(
    preset_key: object,
    width_mm: object | None = None,
    height_mm: object | None = None,
) -> tuple[float, float]:
    """Return the persisted Canvas dimensions for a preset or custom Canvas."""
    key = normalize_canvas_preset(preset_key)
    if key != "custom":
        return CANVAS_PRESETS[key]
    if not _is_finite_number(width_mm) or not _is_finite_number(height_mm):
        raise ValueError("custom Canvas dimensions must be finite numbers")
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("custom Canvas dimensions must be positive")
    return float(width_mm), float(height_mm)


@dataclass(frozen=True)
class FrameGeometry:
    id: int | str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float

    @property
    def frame_id(self) -> int | str:
        return self.id

    @property
    def x(self) -> float:
        return self.x_mm

    @property
    def y(self) -> float:
        return self.y_mm

    @property
    def w(self) -> float:
        return self.width_mm

    @property
    def h(self) -> float:
        return self.height_mm

    @property
    def width(self) -> float:
        return self.width_mm

    @property
    def height(self) -> float:
        return self.height_mm


@dataclass(frozen=True)
class FrameRenderSpec:
    id: int | str
    image: object | None = None
    visible: bool = True
    label: str = ""
    date_label: str | None = None
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0

    @property
    def frame_id(self) -> int | str:
        return self.id


@dataclass(frozen=True)
class CanvasRenderSpec:
    width_mm: float
    height_mm: float
    frame_ratio: float
    columns: int
    frames: tuple[FrameRenderSpec, ...] = field(default_factory=tuple)
    title: str = ""
    patient_id: str = ""
    patient_name: str = ""
    birth_year: int | str | None = None
    show_patient_id: bool = False
    show_patient_name: bool = False
    show_birth_year: bool = False
    background: str = "#ffffff"
    version: int | None = None

    def __post_init__(self) -> None:
        # A render spec is a value object: callers cannot mutate its frame
        # order or crop state after the persisted-version snapshot is read.
        object.__setattr__(self, "frames", tuple(self.frames))


# Names used by callers that describe the same persistence-independent data.
CanvasSpec = CanvasRenderSpec
FrameSpec = FrameRenderSpec
RenderSpec = CanvasRenderSpec


def _render_value(value: object, name: str, default: object = None) -> object:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _render_frame(value: object, index: int) -> FrameRenderSpec:
    if isinstance(value, FrameRenderSpec):
        frame = value
    else:
        frame_id = _render_value(value, "id", _render_value(value, "frame_id", index))
        frame = FrameRenderSpec(
            id=frame_id,
            image=_render_value(value, "image", _render_value(value, "source")),
            visible=_render_value(value, "visible", True),
            label=_render_value(value, "label", "") or "",
            date_label=_render_value(value, "date_label"),
            zoom=_render_value(value, "zoom", 1.0),
            pan_x=_render_value(value, "pan_x", 0.0),
            pan_y=_render_value(value, "pan_y", 0.0),
        )
    if not isinstance(frame.visible, bool):
        raise ValueError("Frame visibility must be boolean")
    if not isinstance(frame.label, str) or len(frame.label) > 500:
        raise ValueError("Frame label is invalid")
    try:
        frame.label.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Frame label contains invalid Unicode") from exc
    if frame.date_label is not None and not isinstance(frame.date_label, str):
        raise ValueError("Frame date label is invalid")
    _crop_number(frame.zoom, "zoom", 1.0, 5.0)
    _crop_number(frame.pan_x, "pan_x", -1.0, 1.0)
    _crop_number(frame.pan_y, "pan_y", -1.0, 1.0)
    return frame


def _render_spec(value: object) -> CanvasRenderSpec:
    if isinstance(value, CanvasRenderSpec):
        spec = value
    else:
        raw_frames = _render_value(value, "frames", []) or []
        try:
            frame_values = tuple(raw_frames)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError("Frames must be a list or tuple") from exc
        spec = CanvasRenderSpec(
            width_mm=_render_value(value, "width_mm"),
            height_mm=_render_value(value, "height_mm"),
            frame_ratio=_render_value(value, "frame_ratio"),
            columns=_render_value(value, "columns"),
            frames=tuple(
                _render_frame(frame, index)
                for index, frame in enumerate(frame_values)
            ),
            title=_render_value(value, "title", "") or "",
            patient_id=_render_value(value, "patient_id", "") or "",
            patient_name=_render_value(value, "patient_name", "") or "",
            birth_year=_render_value(value, "birth_year"),
            show_patient_id=_render_value(value, "show_patient_id", False),
            show_patient_name=_render_value(value, "show_patient_name", False),
            show_birth_year=_render_value(value, "show_birth_year", False),
            background=_render_value(value, "background", "#ffffff"),
            version=_render_value(value, "version"),
        )
    if not _is_finite_number(spec.width_mm) or spec.width_mm <= 0:
        raise ValueError("Canvas width must be a positive finite number")
    if not _is_finite_number(spec.height_mm) or spec.height_mm <= 0:
        raise ValueError("Canvas height must be a positive finite number")
    if not _is_finite_number(spec.frame_ratio) or spec.frame_ratio <= 0:
        raise ValueError("Frame ratio must be a positive finite number")
    if not _is_integer(spec.columns) or not 1 <= spec.columns <= 100:
        raise ValueError("Columns must be a positive integer")
    if not isinstance(spec.frames, tuple):
        raise ValueError("Frames must be a list or tuple")
    if not isinstance(spec.title, str) or len(spec.title) > MAX_TITLE_CHARS:
        raise ValueError("Set title is invalid")
    if not _is_color(spec.background):
        raise ValueError("Canvas background is invalid")
    for field_name in ("show_patient_id", "show_patient_name", "show_birth_year"):
        if not isinstance(getattr(spec, field_name), bool):
            raise ValueError(f"{field_name} must be boolean")
    for index, frame in enumerate(spec.frames):
        _render_frame(frame, index)
    return spec


def _crop_number(value: object, field: str, minimum: float, maximum: float) -> float:
    if not _is_finite_number(value) or not minimum <= float(value) <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return float(value)


def layout_frames(spec: CanvasRenderSpec | object) -> list[FrameGeometry]:
    """Lay out visible Frames as equal rectangles in a centered grid."""
    spec = _render_spec(spec)
    visible = [frame for frame in spec.frames if frame.visible]
    if not visible:
        return []
    rows = math.ceil(len(visible) / spec.columns)
    frame_width = min(spec.width_mm / spec.columns, spec.height_mm / rows * spec.frame_ratio)
    frame_height = frame_width / spec.frame_ratio
    grid_height = rows * frame_height
    top = (spec.height_mm - grid_height) / 2
    result: list[FrameGeometry] = []
    for index, frame in enumerate(visible):
        row = index // spec.columns
        row_count = min(spec.columns, len(visible) - row * spec.columns)
        row_width = row_count * frame_width
        left = (spec.width_mm - row_width) / 2
        result.append(
            FrameGeometry(
                id=frame.id,
                x_mm=left + (index % spec.columns) * frame_width,
                y_mm=top + row * frame_height,
                width_mm=frame_width,
                height_mm=frame_height,
            )
        )
    return result


def cover_crop_normalized(
    image: Image.Image,
    target_width: int,
    target_height: int,
    *,
    zoom: float = 1.0,
    pan_x: float = 0.0,
    pan_y: float = 0.0,
) -> Image.Image:
    """Cover-fit an image using normalized, placement-local crop state."""
    if not _is_integer(target_width) or not _is_integer(target_height) or target_width <= 0 or target_height <= 0:
        raise ValueError("crop target dimensions must be positive integers")
    zoom = _crop_number(zoom, "zoom", 1.0, 5.0)
    pan_x = _crop_number(pan_x, "pan_x", -1.0, 1.0)
    pan_y = _crop_number(pan_y, "pan_y", -1.0, 1.0)

    oriented = ImageOps.exif_transpose(image)
    work = oriented.convert("RGB")
    if oriented is not image:
        oriented.close()
    try:
        source_width, source_height = work.size
        scale = max(target_width / source_width, target_height / source_height) * zoom
        crop_width = max(1, int(round(target_width / scale)))
        crop_height = max(1, int(round(target_height / scale)))
        crop_width = min(source_width, crop_width)
        crop_height = min(source_height, crop_height)
        extra_x = source_width - crop_width
        extra_y = source_height - crop_height
        left = int(round(extra_x * (pan_x + 1.0) / 2.0))
        top = int(round(extra_y * (pan_y + 1.0) / 2.0))
        return work.resize(
            (target_width, target_height),
            Image.Resampling.LANCZOS,
            box=(left, top, left + crop_width, top + crop_height),
        )
    finally:
        work.close()


def _render_source(source: object) -> Image.Image | None:
    if source is None:
        return None
    if isinstance(source, Image.Image):
        oriented = ImageOps.exif_transpose(source)
        result = oriented.convert("RGB")
        if oriented is not source:
            oriented.close()
        return result
    return open_image(source)  # type: ignore[arg-type]


def _draw_render_text(draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], dpi: float, *, bold: bool = False) -> None:
    if not text:
        return
    font = _font_bold(max(1, int(14 * dpi / 72))) if bold else _font(max(1, int(10 * dpi / 72)))
    draw.text(xy, text, fill="#111111", font=font)


def _pixel_edges(
    start_mm: float,
    end_mm: float,
    px: float,
    limit: int,
) -> tuple[int, int]:
    """Convert a positive mm interval to clamped, non-empty pixel edges."""
    start = max(0, min(limit, math.floor(start_mm * px)))
    end = max(0, min(limit, math.ceil(end_mm * px)))
    if end <= start:
        raise ValueError("frame geometry has no pixels at this DPI")
    return start, end


def render_canvas(spec: CanvasRenderSpec | object, dpi: float = 300) -> Image.Image:
    """Render a persisted Set-shaped spec synchronously into a new image."""
    if not _is_finite_number(dpi) or not 1 <= dpi <= MAX_RENDER_DPI:
        raise ValueError(f"dpi must be finite and between 1 and {MAX_RENDER_DPI}")
    spec = _render_spec(spec)
    px = dpi / 25.4
    width = int(spec.width_mm * px)
    height = int(spec.height_mm * px)
    if width <= 0 or height <= 0:
        raise ValueError("Canvas has no pixels at this DPI")
    if width * height > MAX_CANVAS_PIXELS:
        raise ValueError(f"canvas exceeds {MAX_CANVAS_PIXELS} pixels")

    board = Image.new("RGB", (width, height), spec.background)
    board.info["canvas_size_mm"] = (spec.width_mm, spec.height_mm)
    draw = ImageDraw.Draw(board)
    geometries = layout_frames(spec)
    visible = [frame for frame in spec.frames if frame.visible]
    by_id = {frame.id: frame for frame in visible}
    for geometry in geometries:
        frame = by_id[geometry.id]
        left, right = _pixel_edges(
            geometry.x_mm,
            geometry.x_mm + geometry.width_mm,
            px,
            width,
        )
        top, bottom = _pixel_edges(
            geometry.y_mm,
            geometry.y_mm + geometry.height_mm,
            px,
            height,
        )
        x, y = left, top
        target_width, target_height = right - left, bottom - top
        source = _render_source(frame.image)
        if source is None:
            draw.rectangle(
                (x, y, x + target_width - 1, y + target_height - 1),
                fill="#f0f0f0",
                outline="#bbbbbb",
            )
        else:
            try:
                cell = cover_crop_normalized(
                    source,
                    target_width,
                    target_height,
                    zoom=frame.zoom,
                    pan_x=frame.pan_x,
                    pan_y=frame.pan_y,
                )
                try:
                    board.paste(cell, (x, y))
                finally:
                    cell.close()
            finally:
                source.close()
        draw.rectangle(
            (x, y, x + target_width - 1, y + target_height - 1),
            outline="#222222",
            width=max(1, int(round(dpi / 150))),
        )
        caption = " / ".join(
            value for value in (frame.label, frame.date_label) if isinstance(value, str) and value
        )
        if caption:
            _draw_render_text(
                draw,
                caption,
                (x + max(2, int(round(dpi / 25.4))), y + target_height - max(2, int(round(dpi / 25.4)))),
                dpi,
            )

    output_lines = [spec.title]
    patient_values = []
    if spec.show_patient_id and spec.patient_id:
        patient_values.append(spec.patient_id)
    if spec.show_patient_name and spec.patient_name:
        patient_values.append(spec.patient_name)
    if spec.show_birth_year and spec.birth_year is not None:
        patient_values.append(str(spec.birth_year))
    if patient_values:
        output_lines.append(" · ".join(patient_values))
    for index, line in enumerate(value for value in output_lines if value):
        _draw_render_text(
            draw,
            line,
            (
                max(2, int(round(5 * px))),
                max(2, int(round(4 * px))) + index * max(1, int(round(14 * dpi / 72))),
            ),
            dpi,
            bold=index == 0,
        )
    return board


def encode(
    image: Image.Image,
    format: str = "PNG",
    *,
    dpi: float = 300,
    physical_size_mm: tuple[float, float] | None = None,
) -> bytes:
    """Encode a rendered Canvas without writing it to a filesystem path."""
    if not isinstance(format, str):
        raise ValueError("output format is invalid")
    normalized = format.strip().lower().lstrip(".")
    if normalized not in {"png", "pdf", "jpg", "jpeg", "webp"}:
        raise ValueError("output format must be PNG, PDF, JPEG, or WebP")
    if not _is_finite_number(dpi) or not 1 <= dpi <= MAX_RENDER_DPI:
        raise ValueError(f"dpi must be finite and between 1 and {MAX_RENDER_DPI}")
    if physical_size_mm is None:
        physical_size_mm = image.info.get("canvas_size_mm")
    output = io.BytesIO()
    if normalized == "pdf":
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        try:
            if physical_size_mm is None:
                rgb.save(output, format="PDF", resolution=float(dpi))
            else:
                if (
                    not isinstance(physical_size_mm, tuple)
                    or len(physical_size_mm) != 2
                    or not all(_is_finite_number(value) and value > 0 for value in physical_size_mm)
                ):
                    raise ValueError("physical Canvas dimensions must be positive finite numbers")
                width_mm, height_mm = physical_size_mm
                if image.width <= 0 or image.height <= 0:
                    raise ValueError("image dimensions must be positive")
                # Pillow derives PDF MediaBox from pixels / DPI.  Use the
                # effective DPI for the rounded raster so the persisted Canvas
                # dimensions, rather than the raster approximation, are exact.
                effective_dpi = (
                    image.width * 25.4 / float(width_mm),
                    image.height * 25.4 / float(height_mm),
                )
                rgb.save(output, format="PDF", dpi=effective_dpi)
        finally:
            if rgb is not image:
                rgb.close()
    else:
        image.save(output, format={"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}[normalized])
    return output.getvalue()
