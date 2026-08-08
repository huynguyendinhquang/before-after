"""Render a case board: title + clipped image slots (PowerCLIP-style)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.image_policy import open_image

MM = 300 / 25.4  # px per mm at 300 DPI


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
        slots = [Slot(**s) for s in data.get("slots", [])]
        return cls(
            name=data.get("name", Path(path).stem),
            width_mm=data["width_mm"],
            height_mm=data["height_mm"],
            background=data.get("background", "#ffffff"),
            title=data.get("title", {}),
            slots=slots,
            border_px=data.get("border_px", 2),
            border_color=data.get("border_color", "#222222"),
            gap_label_mm=data.get("gap_label_mm", 1.5),
            label_pt=data.get("label_pt", 11),
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
    px = dpi / 25.4
    W = int(template.width_mm * px)
    H = int(template.height_mm * px)
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

    for slot in template.slots:
        x = int(slot.x * px)
        y = int(slot.y * px)
        w = int(slot.w * px)
        h = int(slot.h * px)

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
