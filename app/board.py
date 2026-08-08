"""Render a case board: title + clipped image slots (PowerCLIP-style)."""

from __future__ import annotations

import json
import io
import math
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

from app.image_policy import open_image

MM = 300 / 25.4  # px per mm at 300 DPI
MAX_TITLE_CHARS = 500
MAX_RENDER_DPI = 600
MAX_CANVAS_PIXELS = 100_000_000
MAX_BORDER_PX = 100
MAX_FONT_PT = 200


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


@dataclass
class Slot:
    id: str
    x: float  # mm
    y: float
    w: float
    h: float
    label: str = ""
    fit: str = "cover"  # cover | contain


@dataclass
class BoardTemplate:
    name: str
    width_mm: float
    height_mm: float
    background: str = "#ffffff"
    title: dict[str, Any] = field(default_factory=dict)
    slots: list[Slot] = field(default_factory=list)
    border_px: int = 2
    border_color: str = "#222222"
    gap_label_mm: float = 1.5
    label_pt: int = 11

    @classmethod
    def load(cls, path: str | Path) -> "BoardTemplate":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("template JSON must be an object")
        if "width_mm" not in data or "height_mm" not in data:
            raise ValueError("template JSON requires width_mm and height_mm")
        if not _is_finite_number(data["width_mm"]) or not _is_finite_number(data["height_mm"]):
            raise ValueError("template width_mm and height_mm must be finite numbers")
        width_mm = data["width_mm"]
        height_mm = data["height_mm"]
        if width_mm <= 0 or height_mm <= 0:
            raise ValueError("template width_mm and height_mm must be positive")

        name = data.get("name", Path(path).stem)
        if not isinstance(name, str):
            raise ValueError("template name must be a string")

        background = data.get("background", "#ffffff")
        if not _is_color(background):
            raise ValueError("template background must be a valid color")
        border_color = data.get("border_color", "#222222")
        if not _is_color(border_color):
            raise ValueError("template border_color must be a valid color")

        border_px = data.get("border_px", 2)
        if not _is_integer(border_px) or not 0 <= border_px <= MAX_BORDER_PX:
            raise ValueError(f"template border_px must be an integer from 0 to {MAX_BORDER_PX}")
        label_pt = data.get("label_pt", 11)
        if not _is_integer(label_pt) or not 1 <= label_pt <= MAX_FONT_PT:
            raise ValueError(f"template label_pt must be an integer from 1 to {MAX_FONT_PT}")
        gap_label_mm = data.get("gap_label_mm", 1.5)
        if not _is_finite_number(gap_label_mm) or gap_label_mm < 0:
            raise ValueError("template gap_label_mm must be a finite non-negative number")

        title = data.get("title", {})
        if not isinstance(title, dict):
            raise ValueError("template title must be an object")
        title_allowed = {"x_mm", "y_mm", "pt", "color", "default"}
        unexpected_title = title.keys() - title_allowed
        if unexpected_title:
            raise ValueError(
                "template title has unexpected fields: "
                + ", ".join(sorted(unexpected_title))
            )
        title_x = title.get("x_mm", 5)
        title_y = title.get("y_mm", 4)
        if (
            not _is_finite_number(title_x)
            or title_x < 0
            or title_x > width_mm
            or not _is_finite_number(title_y)
            or title_y < 0
            or title_y > height_mm
        ):
            raise ValueError("template title x_mm and y_mm must be within the canvas")
        title_pt = title.get("pt", 16)
        if not _is_finite_number(title_pt) or not 0 < title_pt <= MAX_FONT_PT:
            raise ValueError(f"template title pt must be between 1 and {MAX_FONT_PT}")
        title_color = title.get("color", "#111111")
        if not _is_color(title_color):
            raise ValueError("template title color must be a valid color")
        title_default = title.get("default", "")
        if not isinstance(title_default, str) or len(title_default) > MAX_TITLE_CHARS:
            raise ValueError(f"template title default exceeds {MAX_TITLE_CHARS} characters")

        raw_slots = data.get("slots", [])
        if not isinstance(raw_slots, list) or any(not isinstance(slot, dict) for slot in raw_slots):
            raise ValueError("template slots must be a list of objects")

        required = {"id", "x", "y", "w", "h"}
        allowed = required | {"label", "fit"}
        slot_ids: set[str] = set()
        slots: list[Slot] = []
        for index, slot in enumerate(raw_slots):
            missing = required - slot.keys()
            if missing:
                raise ValueError(
                    f"template slot {index} missing required fields: {', '.join(sorted(missing))}"
                )
            unexpected = slot.keys() - allowed
            if unexpected:
                raise ValueError(
                    f"template slot {index} has unexpected fields: {', '.join(sorted(unexpected))}"
                )

            slot_id = slot["id"]
            if not isinstance(slot_id, str) or not slot_id:
                raise ValueError(f"template slot {index} id must be a non-empty string")
            if slot_id in slot_ids:
                raise ValueError("template slot IDs must be unique")
            slot_ids.add(slot_id)

            if not _is_finite_number(slot["x"]) or not _is_finite_number(slot["y"]):
                raise ValueError(f"template slot {index} x/y must be finite numbers")
            if (
                not _is_finite_number(slot["w"])
                or not _is_finite_number(slot["h"])
                or slot["w"] <= 0
                or slot["h"] <= 0
            ):
                raise ValueError(f"template slot {index} w/h must be finite positive numbers")
            if (
                slot["x"] < 0
                or slot["y"] < 0
                or slot["x"] + slot["w"] > width_mm
                or slot["y"] + slot["h"] > height_mm
            ):
                raise ValueError(f"template slot {index} must lie within the canvas")
            if not isinstance(slot.get("label", ""), str):
                raise ValueError(f"template slot {index} label must be a string")
            if not isinstance(slot.get("fit", "cover"), str) or slot.get("fit", "cover") not in {
                "cover",
                "contain",
            }:
                raise ValueError(f"template slot {index} fit must be cover or contain")
            slots.append(Slot(**slot))

        return cls(
            name=name,
            width_mm=width_mm,
            height_mm=height_mm,
            background=background,
            title=title,
            slots=slots,
            border_px=border_px,
            border_color=border_color,
            gap_label_mm=gap_label_mm,
            label_pt=label_pt,
        )


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


def cover_crop(
    img: Image.Image,
    tw: int,
    th: int,
    *,
    zoom: float = 1.0,
    pan_x: float = 0.0,
    pan_y: float = 0.0,
) -> Image.Image:
    """Scale image to cover (tw, th) and optionally apply normalized crop state."""
    if zoom != 1.0 or pan_x != 0.0 or pan_y != 0.0:
        return cover_crop_normalized(img, tw, th, zoom=zoom, pan_x=pan_x, pan_y=pan_y)
    img = img.convert("RGB")
    sw, sh = img.size
    scale = max(tw / sw, th / sh)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - tw) // 2
    top = (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def contain_fit(img: Image.Image, tw: int, th: int, bg: str = "#000000") -> Image.Image:
    """Scale image to fit inside (tw, th), letterbox."""
    img = img.convert("RGB")
    sw, sh = img.size
    scale = min(tw / sw, th / sh)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (tw, th), bg)
    canvas.paste(img, ((tw - nw) // 2, (th - nh) // 2))
    return canvas


def place(img: Image.Image, tw: int, th: int, fit: str = "cover") -> Image.Image:
    if fit == "contain":
        return contain_fit(img, tw, th)
    return cover_crop(img, tw, th)


@dataclass
class CaseData:
    title: str
    images: dict[str, str | Path]  # slot_id -> path
    labels: dict[str, str] = field(default_factory=dict)  # slot_id -> caption


def render(template: BoardTemplate, case: CaseData, dpi: float = 300) -> Image.Image:
    if not _is_finite_number(dpi) or dpi < 1 or dpi > MAX_RENDER_DPI:
        raise ValueError(f"dpi must be finite and between 1 and {MAX_RENDER_DPI}")

    px = dpi / 25.4
    try:
        W = int(template.width_mm * px)
        H = int(template.height_mm * px)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("canvas dimensions are invalid") from exc
    if W <= 0 or H <= 0:
        raise ValueError("canvas must have nonzero pixels")
    if W * H > MAX_CANVAS_PIXELS:
        raise ValueError(f"canvas exceeds {MAX_CANVAS_PIXELS} pixels")

    frames: list[tuple[int, int, int, int]] = []
    for slot in template.slots:
        try:
            x = int(slot.x * px)
            y = int(slot.y * px)
            w = int(slot.w * px)
            h = int(slot.h * px)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"frame {slot.id} geometry is invalid") from exc
        if w <= 0 or h <= 0:
            raise ValueError(f"frame {slot.id} must have nonzero pixels")
        if x < 0 or y < 0 or x + w > W or y + h > H:
            raise ValueError(f"frame {slot.id} must lie within the canvas")
        frames.append((x, y, w, h))

    board = Image.new("RGB", (W, H), template.background)
    draw = ImageDraw.Draw(board)

    # Title
    tcfg = template.title or {}
    title_text = case.title or tcfg.get("default", "")
    if title_text:
        tx = int(tcfg.get("x_mm", 5) * px)
        ty = int(tcfg.get("y_mm", 4) * px)
        tsize = int(tcfg.get("pt", 16) * dpi / 72)
        font = _font_bold(tsize)
        color = tcfg.get("color", "#111111")
        draw.text((tx, ty), title_text, fill=color, font=font)

    label_font = _font(int(template.label_pt * dpi / 72))
    gap = int(template.gap_label_mm * px)

    for slot, (x, y, w, h) in zip(template.slots, frames):
        path = case.images.get(slot.id)
        if path:
            src = open_image(path)
            try:
                cell = place(src, w, h, slot.fit)
            finally:
                src.close()
            board.paste(cell, (x, y))
        else:
            # empty placeholder
            draw.rectangle([x, y, x + w - 1, y + h - 1], fill="#f0f0f0", outline="#bbbbbb")
            ph = _font(max(12, h // 12))
            msg = slot.id
            bbox = draw.textbbox((0, 0), msg, font=ph)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text((x + (w - tw) // 2, y + (h - th) // 2), msg, fill="#888888", font=ph)

        if template.border_px > 0:
            b = template.border_px
            draw.rectangle([x, y, x + w - 1, y + h - 1], outline=template.border_color, width=b)

        caption = case.labels.get(slot.id, slot.label)
        if caption:
            bbox = draw.textbbox((0, 0), caption, font=label_font)
            tw = bbox[2] - bbox[0]
            draw.text((x + (w - tw) // 2, y + h + gap), caption, fill="#222222", font=label_font)

    return board


def export(board: Image.Image, out: str | Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    suffix = out.suffix.lower()
    if suffix == ".pdf":
        rgb = board.convert("RGB")
        rgb.save(out, "PDF", resolution=300.0)
    else:
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            out = out.with_suffix(".png")
        board.save(out)
    return out


# Slice 4 renderer ---------------------------------------------------------
#
# These objects intentionally contain no ORM or Flask types.  The comparison
# workflow turns its rows into this small render specification before calling
# the functions below.  The legacy BoardTemplate/CaseData renderer above is
# kept until the Slice 5 export cut-over.

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


@dataclass
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


@dataclass
class CanvasRenderSpec:
    width_mm: float
    height_mm: float
    frame_ratio: float
    columns: int
    frames: list[FrameRenderSpec] = field(default_factory=list)
    title: str = ""
    patient_id: str = ""
    patient_name: str = ""
    birth_year: int | str | None = None
    show_patient_id: bool = False
    show_patient_name: bool = False
    show_birth_year: bool = False
    background: str = "#ffffff"
    version: int | None = None


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
        spec = CanvasRenderSpec(
            width_mm=_render_value(value, "width_mm"),
            height_mm=_render_value(value, "height_mm"),
            frame_ratio=_render_value(value, "frame_ratio"),
            columns=_render_value(value, "columns"),
            frames=[
                _render_frame(frame, index)
                for index, frame in enumerate(_render_value(value, "frames", []) or [])
            ],
            title=_render_value(value, "title", "") or "",
            patient_id=_render_value(value, "patient_id", "") or "",
            patient_name=_render_value(value, "patient_name", "") or "",
            birth_year=_render_value(value, "birth_year"),
            show_patient_id=_render_value(value, "show_patient_id", False),
            show_patient_name=_render_value(value, "show_patient_name", False),
            show_birth_year=_render_value(value, "show_birth_year", False),
            background=_render_value(value, "background", "#ffffff"),
        )
    if not _is_finite_number(spec.width_mm) or spec.width_mm <= 0:
        raise ValueError("Canvas width must be a positive finite number")
    if not _is_finite_number(spec.height_mm) or spec.height_mm <= 0:
        raise ValueError("Canvas height must be a positive finite number")
    if not _is_finite_number(spec.frame_ratio) or spec.frame_ratio <= 0:
        raise ValueError("Frame ratio must be a positive finite number")
    if not _is_integer(spec.columns) or not 1 <= spec.columns <= 100:
        raise ValueError("Columns must be a positive integer")
    if not isinstance(spec.frames, list):
        raise ValueError("Frames must be a list")
    if not isinstance(spec.title, str) or len(spec.title) > MAX_TITLE_CHARS:
        raise ValueError("Set title is invalid")
    if not _is_color(spec.background):
        raise ValueError("Canvas background is invalid")
    for field_name in ("show_patient_id", "show_patient_name", "show_birth_year"):
        if not isinstance(getattr(spec, field_name), bool):
            raise ValueError(f"{field_name} must be boolean")
    spec.frames = [_render_frame(frame, index) for index, frame in enumerate(spec.frames)]
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


def encode(image: Image.Image, format: str = "PNG") -> bytes:
    """Encode a rendered Canvas without writing it to a filesystem path."""
    if not isinstance(format, str):
        raise ValueError("output format is invalid")
    normalized = format.strip().lower().lstrip(".")
    if normalized not in {"png", "pdf", "jpg", "jpeg", "webp"}:
        raise ValueError("output format must be PNG, PDF, JPEG, or WebP")
    output = io.BytesIO()
    if normalized == "pdf":
        image.convert("RGB").save(output, format="PDF", resolution=300.0)
    else:
        image.save(output, format={"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}[normalized])
    return output.getvalue()
