"""Render a case board: title + clipped image slots (PowerCLIP-style)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageColor, ImageDraw, ImageFont

from app.image_policy import open_image

MM = 300 / 25.4  # px per mm at 300 DPI
MAX_TITLE_CHARS = 500
MAX_RENDER_DPI = 600
MAX_CANVAS_PIXELS = 100_000_000
MAX_BORDER_PX = 100
MAX_FONT_PT = 200


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
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


def cover_crop(img: Image.Image, tw: int, th: int) -> Image.Image:
    """Scale image to cover (tw, th) and center-crop — PowerCLIP fill."""
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
