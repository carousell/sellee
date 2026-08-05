"""Photo conversion, against real image files rather than a stubbed converter.

The reason this is worth testing for real: the formats a seller's phone produces are exactly the
ones a marketplace refuses, and the conversion is now ours rather than the operating system's.
"""

from __future__ import annotations

import pytest
from PIL import Image

from selly_agent import images


def _write(path, mode="RGB", size=(40, 30), fmt=None, colour=(10, 120, 200)):
    image = Image.new(mode, size, colour if mode in ("RGB", "RGBA") else 128)
    image.save(path, fmt)
    return path


def test_a_heic_becomes_a_jpeg(tmp_path) -> None:
    """The case the old sips-only path could not serve off macOS: an iPhone's default format."""
    source = tmp_path / "photo.heic"
    Image.new("RGB", (50, 40), (200, 30, 30)).save(source, "HEIF")

    images.to_jpeg(source, tmp_path / "out.jpg", 2048)

    with Image.open(tmp_path / "out.jpg") as converted:
        assert converted.format == "JPEG"
        assert converted.size == (50, 40)


def test_an_oversized_photo_is_scaled_to_fit_the_limit(tmp_path) -> None:
    source = _write(tmp_path / "big.png", size=(4000, 3000), fmt="PNG")

    images.to_jpeg(source, tmp_path / "out.jpg", 1600)

    with Image.open(tmp_path / "out.jpg") as converted:
        assert max(converted.size) == 1600
        assert converted.size == (1600, 1200)  # aspect ratio held


def test_a_small_photo_is_never_enlarged(tmp_path) -> None:
    """Upscaling would cost bytes on the upload without adding any detail."""
    source = _write(tmp_path / "small.png", size=(120, 90), fmt="PNG")

    images.to_jpeg(source, tmp_path / "out.jpg", 2048)

    with Image.open(tmp_path / "out.jpg") as converted:
        assert converted.size == (120, 90)


def test_transparency_is_flattened_rather_than_refused(tmp_path) -> None:
    """A screenshot or a PNG export carries alpha, which JPEG has no channel for."""
    source = tmp_path / "logo.png"
    Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(source, "PNG")

    images.to_jpeg(source, tmp_path / "out.jpg", 2048)

    with Image.open(tmp_path / "out.jpg") as converted:
        assert converted.mode == "RGB"
        assert converted.getpixel((0, 0)) == (255, 255, 255)


def test_a_greyscale_photo_converts(tmp_path) -> None:
    source = _write(tmp_path / "grey.png", mode="L", fmt="PNG")

    images.to_jpeg(source, tmp_path / "out.jpg", 2048)

    with Image.open(tmp_path / "out.jpg") as converted:
        assert converted.mode == "RGB"


def test_an_exif_rotation_is_applied(tmp_path) -> None:
    """A portrait phone photo is stored landscape with an orientation tag; ignoring it uploads a
    photo on its side."""
    source = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (60, 20), (10, 10, 10))
    exif = image.getexif()
    exif[274] = 6  # rotate 90° clockwise
    image.save(source, "JPEG", exif=exif)

    images.to_jpeg(source, tmp_path / "out.jpg", 2048)

    with Image.open(tmp_path / "out.jpg") as converted:
        assert converted.size == (20, 60)


def test_something_that_is_not_an_image_names_the_file(tmp_path) -> None:
    """The message reaches the seller through an escalation, so it has to say which photo."""
    source = tmp_path / "notes.heic"
    source.write_bytes(b"this is not an image")

    with pytest.raises(images.ImageToolUnavailable, match="notes.heic"):
        images.to_jpeg(source, tmp_path / "out.jpg", 2048)


def test_the_destination_directory_is_created(tmp_path) -> None:
    source = _write(tmp_path / "a.png", fmt="PNG")

    images.to_jpeg(source, tmp_path / "nested" / "deeper" / "out.jpg", 2048)

    assert (tmp_path / "nested" / "deeper" / "out.jpg").is_file()
