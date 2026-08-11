"""The photo converter, against real image bytes.

Every fixture is generated in-test and decoded back: what the pipeline depends on is the pixels
that come out — orientation applied, never enlarged, always a JPEG — and an argv assertion could
pin none of that. HEIC gets its own cases as the format a phone actually produces.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from selly_agent.platform.base import ImageToolUnavailable
from selly_agent.platform.container import ContainerPlatform
from selly_agent.platform.images import to_jpeg
from selly_agent.platform.macos import MacOSPlatform


def _exif_orientation(value: int) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = value
    return exif.tobytes()


def _write(path: Path, size, *, fmt="JPEG", mode="RGB", color="red", orientation=None) -> Path:
    image = Image.new(mode, size, color)
    kwargs = {"exif": _exif_orientation(orientation)} if orientation is not None else {}
    image.save(path, format=fmt, **kwargs)
    return path


def _opened(path: Path):
    with Image.open(path) as image:
        image.load()
        return image


def _detailed(path: Path, size=(400, 300)) -> Path:
    """A source with enough detail that the encoder's quality setting shows in the output. A flat
    colour would compress to nothing at any quality and pin nothing."""
    image = Image.new("RGB", size)
    image.putdata(
        [
            ((x * 7) % 256, (y * 13) % 256, (x * y) % 256)
            for y in range(size[1])
            for x in range(size[0])
        ]
    )
    image.save(path, format="PNG")
    return path


# --- the transform ----------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "HEIF"])
def test_every_intake_format_comes_out_a_jpeg(tmp_path, fmt) -> None:
    src = _write(tmp_path / "a.in", (800, 600), fmt=fmt)
    dest = tmp_path / "out" / "a.jpg"

    to_jpeg(src, dest, 1600)

    converted = _opened(dest)
    assert converted.format == "JPEG"
    assert converted.size == (800, 600)


def test_an_oversized_photo_is_shrunk_to_the_bound_keeping_its_shape(tmp_path) -> None:
    src = _write(tmp_path / "big.jpg", (4000, 3000))
    to_jpeg(src, tmp_path / "big-out.jpg", 1600)
    assert _opened(tmp_path / "big-out.jpg").size == (1600, 1200)


def test_a_photo_already_inside_the_bound_is_never_enlarged(tmp_path) -> None:
    """Shrink-only: blowing a small photo up to meet the limit would add nothing but bytes."""
    src = _write(tmp_path / "small.jpg", (320, 240))
    to_jpeg(src, tmp_path / "small-out.jpg", 1600)
    assert _opened(tmp_path / "small-out.jpg").size == (320, 240)


@pytest.mark.parametrize("fmt", ["JPEG", "HEIF"])
def test_exif_orientation_is_baked_into_the_pixels(tmp_path, fmt) -> None:
    """A phone writes the picture unrotated and records which way is up in a tag. A marketplace
    that ignores the tag shows the listing sideways, so the rotation is applied before upload."""
    src = _write(tmp_path / "rotated.in", (120, 40), fmt=fmt, orientation=6)
    to_jpeg(src, tmp_path / "rotated.jpg", 1600)
    assert _opened(tmp_path / "rotated.jpg").size == (40, 120)


def test_photos_are_not_written_at_the_encoders_default_quality(tmp_path) -> None:
    """Pillow defaults to quality 75, below what sips and ImageMagick produced — a silent
    downgrade of every listing photo, on the OS where the pipeline already worked. Compared
    against a default-quality encode of the same pixels rather than a byte count that would
    drift with the library."""
    src = _detailed(tmp_path / "detail.png")
    to_jpeg(src, tmp_path / "converted.jpg", 1600)

    default_quality = tmp_path / "default.jpg"
    _opened(src).save(default_quality, format="JPEG")

    assert (tmp_path / "converted.jpg").stat().st_size > default_quality.stat().st_size


def test_transparency_is_flattened_rather_than_refused(tmp_path) -> None:
    """JPEG carries no alpha channel, so a PNG with one has to be coerced — left alone it fails
    at the last step, after the conversion looked like it was working."""
    src = _write(tmp_path / "alpha.png", (200, 100), fmt="PNG", mode="RGBA", color=(0, 128, 0, 90))
    to_jpeg(src, tmp_path / "alpha.jpg", 1600)
    assert _opened(tmp_path / "alpha.jpg").mode == "RGB"


def test_a_multi_frame_source_yields_the_primary_image_only(tmp_path) -> None:
    """A burst or a depth map travels beside the picture; the caller was promised one file at one
    path, so the extra frames stay where they are."""
    frames = [Image.new("RGB", (200, 100), c) for c in ("red", "blue", "green")]
    src = tmp_path / "burst.gif"
    frames[0].save(src, format="GIF", save_all=True, append_images=frames[1:])

    to_jpeg(src, tmp_path / "burst.jpg", 1600)

    converted = _opened(tmp_path / "burst.jpg")
    assert converted.format == "JPEG"
    assert converted.size == (200, 100)


def test_the_destination_directory_is_created(tmp_path) -> None:
    src = _write(tmp_path / "a.jpg", (100, 100))
    to_jpeg(src, tmp_path / "deep" / "nested" / "a.jpg", 1600)
    assert (tmp_path / "deep" / "nested" / "a.jpg").is_file()


# --- failure ----------------------------------------------------------------------------------


def test_a_corrupt_file_names_the_file_it_could_not_convert(tmp_path) -> None:
    src = tmp_path / "truncated.heic"
    src.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"not actually an image")
    with pytest.raises(ImageToolUnavailable, match="truncated.heic"):
        to_jpeg(src, tmp_path / "out.jpg", 1600)


def test_a_missing_file_names_the_file_it_could_not_convert(tmp_path) -> None:
    with pytest.raises(ImageToolUnavailable, match="ghost.jpg"):
        to_jpeg(tmp_path / "ghost.jpg", tmp_path / "out.jpg", 1600)


# --- the seam ---------------------------------------------------------------------------------


@pytest.mark.parametrize("platform", [MacOSPlatform(), ContainerPlatform()])
def test_every_platform_converts_through_the_one_implementation(tmp_path, platform) -> None:
    """One converter, so a photo that works on a Mac works in the container and on a Linux
    desktop. The container's other methods refuse; this one does not."""
    src = _write(tmp_path / f"{platform.name}.heic", (2000, 1000), fmt="HEIF")
    dest = tmp_path / f"{platform.name}.jpg"

    platform.to_jpeg(src, dest, 1600)

    converted = _opened(dest)
    assert converted.format == "JPEG"
    assert converted.size == (1600, 800)
