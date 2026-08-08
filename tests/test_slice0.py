from __future__ import annotations

import io
import re
from array import array
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from werkzeug.datastructures import FileStorage

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


@pytest.mark.parametrize("canvas_mm", [(297.0, 210.0), (40.0, 30.0)])
def test_pdf_media_box_matches_canvas_dimensions(canvas_mm: tuple[float, float]) -> None:
    spec = CanvasRenderSpec(
        width_mm=canvas_mm[0],
        height_mm=canvas_mm[1],
        frame_ratio=1,
        columns=1,
    )
    board = render_canvas(spec, dpi=20)
    try:
        pdf = encode(
            board,
            "pdf",
            dpi=20,
            physical_size_mm=canvas_mm,
        )
    finally:
        board.close()

    match = re.search(rb"/MediaBox \[ 0 0 ([0-9.e+-]+) ([0-9.e+-]+) \]", pdf)
    assert match is not None
    expected = tuple(value * 72 / 25.4 for value in canvas_mm)
    assert float(match.group(1)) == pytest.approx(expected[0], abs=1e-9)
    assert float(match.group(2)) == pytest.approx(expected[1], abs=1e-9)


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

    def should_not_open(*args: object, **kwargs: object) -> None:
        pytest.fail("Image.open must not run before the byte limit is checked")

    monkeypatch.setattr(image_policy.Image, "open", should_not_open)
    with pytest.raises(image_policy.ImagePolicyError, match="byte"):
        image_policy.open_image(source)


def test_pixel_limit_preserves_pillow_bomb_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "fixture.jpg"
    write_image(source, "JPEG", size=(4, 2))
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


class _NonSeekableStream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.position = 0
        self.read_sizes: list[int] = []
        self.closed = False

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            size = len(self.payload) - self.position
        end = min(len(self.payload), self.position + size)
        chunk = self.payload[self.position:end]
        self.position = end
        return chunk

    def close(self) -> None:
        self.closed = True


def test_nonseekable_stream_is_bounded_and_not_closed(tmp_path: Path) -> None:
    source = tmp_path / "fixture.png"
    payload = write_image(source, "PNG", size=(32, 16))
    stream = _NonSeekableStream(payload)

    with pytest.raises(image_policy.ImagePolicyError, match="byte"):
        image_policy.open_image(stream, max_bytes=len(payload) - 1)

    assert sum(size for size in stream.read_sizes if size >= 0) <= len(payload)
    assert stream.closed is False


def test_file_storage_cursor_is_restored_and_save_still_works(tmp_path: Path) -> None:
    source = tmp_path / "fixture.png"
    payload = write_image(source, "PNG", size=(32, 16))
    storage = FileStorage(stream=io.BytesIO(payload), filename="fixture.png", content_length=0)

    with pytest.raises(image_policy.ImagePolicyError, match="byte"):
        image_policy.open_image(storage, max_bytes=len(payload) - 1)

    storage.stream.seek(0)
    with image_policy.open_image(storage) as image:
        assert image.size == (32, 16)
    destination = tmp_path / "saved.png"
    storage.save(destination)
    assert destination.read_bytes() == payload


@pytest.mark.parametrize("failure", [False, True])
def test_seekable_external_stream_cursor_is_restored_on_success_and_failure(
    tmp_path: Path, failure: bool
) -> None:
    source = tmp_path / "fixture.png"
    payload = write_image(source, "PNG")
    stream = io.BytesIO(payload)
    stream.seek(3)

    if failure:
        with pytest.raises(image_policy.ImagePolicyError, match="pixel"):
            image_policy.open_image(stream, max_pixels=1)
    else:
        with image_policy.open_image(stream) as image:
            assert image.size == (4, 2)

    assert stream.tell() == 3


class _OversizedChunk(bytearray):
    def __bytes__(self) -> bytes:
        raise AssertionError("oversized chunk must be rejected before coercion")


class _OversizedChunkStream:
    def __init__(self) -> None:
        self.read_sizes: list[int] = []
        self.closed = False

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> _OversizedChunk:
        self.read_sizes.append(size)
        return _OversizedChunk(b"x" * (size + 1))

    def close(self) -> None:
        self.closed = True


def test_bounded_read_rejects_oversized_return_before_retaining_chunk() -> None:
    source = _OversizedChunkStream()

    with pytest.raises(image_policy.ImagePolicyError, match="byte"):
        image_policy.open_image(source, max_bytes=8)

    assert source.read_sizes == [9]
    assert source.closed is False


class _TypedMemoryviewStream:
    def __init__(self) -> None:
        self.chunk = memoryview(array("I", [1, 2]))
        self.read_count = 0

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> memoryview:
        self.read_count += 1
        if self.read_count == 1:
            return self.chunk
        return memoryview(b"")


def test_bounded_read_counts_typed_memoryview_bytes() -> None:
    source = _TypedMemoryviewStream()
    assert source.chunk.nbytes > len(source.chunk)

    with pytest.raises(image_policy.ImagePolicyError, match="exceeds byte limit"):
        image_policy.read_bounded(source, max_bytes=source.chunk.nbytes - 1)


def _corrupted_tiff_payload() -> bytes:
    payload_stream = io.BytesIO()
    Image.new("RGB", (16, 16)).save(payload_stream, format="TIFF")
    payload = bytearray(payload_stream.getvalue())
    for offset, bit in ((452, 7), (806, 4), (604, 0), (72, 3)):
        payload[offset] ^= 1 << bit
    return bytes(payload)


def test_corrupted_tiff_is_rejected() -> None:
    with pytest.raises(image_policy.ImagePolicyError):
        image_policy.open_image(_corrupted_tiff_payload())


def test_decompression_bomb_warning_is_an_image_policy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_stream = io.BytesIO()
    Image.new("RGB", (4, 2), "#447799").save(payload_stream, format="PNG")
    original_bomb_limit = Image.MAX_IMAGE_PIXELS
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 4)

    with pytest.raises(image_policy.ImagePolicyError, match="decompression"):
        image_policy.open_image(payload_stream.getvalue(), max_pixels=100)

    assert Image.MAX_IMAGE_PIXELS == 4
    assert original_bomb_limit != 4


def test_png_without_terminal_iend_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "fixture.png"
    payload = write_image(source, "PNG", size=(16, 16))
    assert payload.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")

    with pytest.raises(image_policy.ImagePolicyError, match="IEND"):
        image_policy.open_image(payload[:-12])


def test_jpeg_without_terminal_eoi_is_rejected() -> None:
    image = Image.new("RGB", (16, 16))
    image.putdata([(x * 31 % 256, y * 47 % 256, (x + y) * 19 % 256) for y in range(16) for x in range(16)])
    payload_stream = io.BytesIO()
    image.save(payload_stream, format="JPEG", quality=95)
    payload = payload_stream.getvalue()
    assert payload.endswith(b"\xff\xd9")

    with pytest.raises(image_policy.ImagePolicyError, match="EOI"):
        image_policy.open_image(payload[:-2])


def test_image_policy_normalizes_io_and_type_failures(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    for source in (tmp_path / "missing.png", directory, object()):
        with pytest.raises(image_policy.ImagePolicyError):
            image_policy.open_image(source)  # type: ignore[arg-type]


def test_request_limit_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(image_policy.MAX_REQUEST_BYTES_ENV, "128")
    assert image_policy.configured_request_limit() == 128


def test_canvas_budget_is_checked_before_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import board as board_module

    def unexpected_allocation(*args: object, **kwargs: object) -> None:
        pytest.fail("render allocated an over-budget image")

    monkeypatch.setattr(board_module.Image, "new", unexpected_allocation)
    spec = CanvasRenderSpec(1000, 1000, 1, 1)
    with pytest.raises(ValueError, match="pixel"):
        render_canvas(spec, dpi=600)


def test_canvas_zero_pixel_budget_is_checked_before_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import board as board_module

    def unexpected_allocation(*args: object, **kwargs: object) -> None:
        pytest.fail("render allocated a zero-pixel image")

    monkeypatch.setattr(board_module.Image, "new", unexpected_allocation)
    spec = CanvasRenderSpec(0.001, 10, 1, 1)
    with pytest.raises(ValueError, match="Canvas"):
        render_canvas(spec, dpi=1)


@pytest.mark.parametrize("dpi", [0, float("nan"), float("inf"), 601])
def test_encode_rejects_invalid_render_budget(dpi: float) -> None:
    image = Image.new("RGB", (1, 1))
    try:
        with pytest.raises(ValueError, match="dpi"):
            encode(image, "pdf", dpi=dpi)
    finally:
        image.close()


def test_render_spec_is_immutable() -> None:
    from app.board import CanvasRenderSpec, FrameRenderSpec

    spec = CanvasRenderSpec(20, 20, 1, 1, [FrameRenderSpec(1)])
    assert isinstance(spec.frames, tuple)
    with pytest.raises(AttributeError):
        spec.frames = ()  # type: ignore[misc]
