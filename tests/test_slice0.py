from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import image_policy
from app.board import BoardTemplate, CaseData, Slot, _font, cover_crop, export, render


def _write_image(path: Path, image_format: str, size: tuple[int, int] = (4, 2), orientation: int | None = None) -> bytes:
    image = Image.new("RGB", size)
    image.putdata([(x * 60, y * 100, 120) for y in range(size[1]) for x in range(size[0])])
    save_kwargs = {}
    if orientation is not None:
        exif = image.getexif()
        exif[274] = orientation
        save_kwargs["exif"] = exif.tobytes()
    image.save(path, format=image_format, **save_kwargs)
    return path.read_bytes()


def test_current_renderer_exports_non_empty_png_and_pdf(tmp_path: Path) -> None:
    source = tmp_path / "generated-fixture.jpg"
    _write_image(source, "JPEG", size=(8, 4))
    template = BoardTemplate(
        name="smoke",
        width_mm=40,
        height_mm=30,
        slots=[Slot(id="fixture", x=2, y=2, w=36, h=20)],
    )

    board = render(template, CaseData(title="Generated fixture", images={"fixture": source}), dpi=72)
    png = export(board, tmp_path / "board.png")
    pdf = export(board, tmp_path / "board.pdf")

    assert png.stat().st_size > 0
    assert pdf.stat().st_size > 0
    assert pdf.read_bytes().startswith(b"%PDF")
    with Image.open(png) as output:
        output.load()
        assert output.size == board.size


def test_web_prototype_path_accepts_generated_fixture() -> None:
    from app.web import app

    payload = io.BytesIO()
    Image.new("RGB", (8, 4), "#447799").save(payload, format="PNG")
    response = app.test_client().post(
        "/render",
        data={
            "title": "Generated fixture",
            "template": "viengut_case",
            "format": "png",
            "slot_portrait": (io.BytesIO(payload.getvalue()), "fixture.not-an-image"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert len(response.data) > 0


@pytest.mark.parametrize(
    ("orientation", "expected_size"),
    [(1, (4, 2)), (6, (2, 4)), (8, (2, 4))],
)
def test_exif_orientation_is_applied_before_geometry_and_crop(
    tmp_path: Path, orientation: int, expected_size: tuple[int, int]
) -> None:
    source = tmp_path / f"orientation-{orientation}.jpg"
    _write_image(source, "JPEG", orientation=orientation)

    oriented = image_policy.open_image(source)
    try:
        assert oriented.size == expected_size
        assert getattr(oriented, "fp", None) is None
        assert cover_crop(oriented, *expected_size).size == expected_size
    finally:
        oriented.close()


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "TIFF", "WEBP"])
def test_supported_format_is_detected_from_content_not_extension(tmp_path: Path, image_format: str) -> None:
    source = tmp_path / "fixture.bin"
    _write_image(source, image_format)

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


def test_byte_limit_is_configurable_and_checked_before_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "fixture.jpg"
    payload = _write_image(source, "JPEG")
    monkeypatch.setenv(image_policy.MAX_BYTES_ENV, str(len(payload) - 1))

    def should_not_open(*args: object, **kwargs: object) -> None:
        pytest.fail("Image.open must not run before the byte limit is checked")

    monkeypatch.setattr(image_policy.Image, "open", should_not_open)
    with pytest.raises(image_policy.ImagePolicyError, match="byte"):
        image_policy.open_image(source)


def test_pixel_limit_is_configurable_without_disabling_pillow_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "fixture.jpg"
    _write_image(source, "JPEG", size=(4, 2))
    original_bomb_limit = Image.MAX_IMAGE_PIXELS
    monkeypatch.setenv(image_policy.MAX_PIXELS_ENV, "7")

    with pytest.raises(image_policy.ImagePolicyError, match="pixel"):
        image_policy.open_image(source)

    assert image_policy.DEFAULT_MAX_BYTES == 50 * 1024 * 1024
    assert image_policy.DEFAULT_MAX_PIXELS == 60_000_000
    assert Image.MAX_IMAGE_PIXELS == original_bomb_limit


def test_installed_font_can_render_vietnamese_text() -> None:
    font = _font(24)
    assert isinstance(font, ImageFont.FreeTypeFont)
    assert Path(font.path).is_file()

    mask = Image.new("L", (260, 48), 0)
    ImageDraw.Draw(mask).text((0, 0), "Nguyễn Văn Ánh", fill=255, font=font)
    assert mask.getbbox() is not None
