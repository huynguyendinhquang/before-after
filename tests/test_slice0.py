from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import image_policy
from app.board import CanvasRenderSpec, FrameRenderSpec, _font, cover_crop_normalized, encode, render_canvas


def write_image(path: Path, image_format: str, size: tuple[int, int] = (4, 2), orientation: int | None = None) -> bytes:
    image = Image.new("RGB", size)
    image.putdata([(x * 60, y * 100, 120) for y in range(size[1]) for x in range(size[0])])
    save_kwargs = {}
    if orientation is not None:
        exif = image.getexif()
        exif[274] = orientation
        save_kwargs["exif"] = exif.tobytes()
    image.save(path, format=image_format, **save_kwargs)
    return path.read_bytes()


def test_board_renderer_exports_png_and_pdf(tmp_path: Path) -> None:
    source = tmp_path / "fixture.jpg"
    write_image(source, "JPEG", size=(8, 4))
    spec = CanvasRenderSpec(
        width_mm=40,
        height_mm=30,
        frame_ratio=1.8,
        columns=1,
        frames=[FrameRenderSpec(1, image=source.read_bytes())],
        title="Generated fixture",
    )
    board = render_canvas(spec, dpi=72)
    try:
        png = encode(board, "png")
        pdf = encode(board, "pdf")
        assert png
        assert pdf.startswith(b"%PDF")
        with Image.open(io.BytesIO(png)) as output:
            output.load()
            assert output.size == board.size
    finally:
        board.close()


@pytest.mark.parametrize(
    ("orientation", "expected_size"),
    [(1, (4, 2)), (6, (2, 4)), (8, (2, 4))],
)
def test_exif_orientation_is_applied_before_geometry_and_crop(
    tmp_path: Path, orientation: int, expected_size: tuple[int, int]
) -> None:
    source = tmp_path / f"orientation-{orientation}.jpg"
    write_image(source, "JPEG", orientation=orientation)
    oriented = image_policy.open_image(source)
    try:
        assert oriented.size == expected_size
        assert getattr(oriented, "fp", None) is None
        crop = cover_crop_normalized(oriented, *expected_size)
        try:
            assert crop.size == expected_size
        finally:
            crop.close()
    finally:
        oriented.close()


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "TIFF", "WEBP", "BMP"])
def test_supported_format_is_detected_from_content(tmp_path: Path, image_format: str) -> None:
    source = tmp_path / "fixture.bin"
    write_image(source, image_format)
    image = image_policy.open_image(source)
    try:
        assert image.size == (4, 2)
    finally:
        image.close()


def test_unknown_content_and_animation_are_rejected(tmp_path: Path) -> None:
    unknown = tmp_path / "unknown.bin"
    unknown.write_bytes(b"not an image")
    with pytest.raises(image_policy.ImagePolicyError):
        image_policy.open_image(unknown)

    frame = Image.new("RGB", (4, 2), "red")
    animated = tmp_path / "animated.webp"
    frame.save(
        animated,
        format="WEBP",
        save_all=True,
        append_images=[Image.new("RGB", (4, 2), "blue")],
        duration=1,
        loop=0,
    )
    with pytest.raises(image_policy.ImagePolicyError, match="animat"):
        image_policy.open_image(animated)


def test_image_limits_are_checked_before_decode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "fixture.jpg"
    payload = write_image(source, "JPEG")
    monkeypatch.setenv(image_policy.MAX_BYTES_ENV, str(len(payload) - 1))
    with pytest.raises(image_policy.ImagePolicyError, match="byte"):
        image_policy.open_image(source)

    monkeypatch.setenv(image_policy.MAX_BYTES_ENV, str(len(payload) + 1))
    monkeypatch.setenv(image_policy.MAX_PIXELS_ENV, "7")
    with pytest.raises(image_policy.ImagePolicyError, match="pixel"):
        image_policy.open_image(source)


def test_installed_font_can_render_vietnamese_text() -> None:
    font = _font(24)
    assert isinstance(font, ImageFont.FreeTypeFont)
    assert Path(font.path).is_file()
    mask = Image.new("L", (260, 48), 0)
    ImageDraw.Draw(mask).text((0, 0), "Nguyễn Văn Ánh", fill=255, font=font)
    assert mask.getbbox() is not None


def test_render_spec_is_immutable() -> None:
    from app.board import CanvasRenderSpec, FrameRenderSpec

    spec = CanvasRenderSpec(20, 20, 1, 1, [FrameRenderSpec(1)])
    assert isinstance(spec.frames, tuple)
    with pytest.raises(AttributeError):
        spec.frames = ()  # type: ignore[misc]
