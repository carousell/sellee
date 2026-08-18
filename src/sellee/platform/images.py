"""The photo converter: one implementation, every platform.

The pipeline needs one transform — whatever a phone or a marketplace hands us, out as a JPEG the
rail will accept, no larger than a bound. HEIC is why a decoder is bundled at all: an iPhone
photo arrives in it, no marketplace takes it, and Pillow does not read it without pillow-heif.
"""

from __future__ import annotations

from pathlib import Path

from sellee.platform.base import ImageToolUnavailable

_opener_registered = False

# Pillow defaults to 75, which is visibly soft on a photo of an item someone is trying to sell.
# ImageMagick wrote these at 92 and sips at its own high default, so this keeps what the OS tools
# produced rather than quietly downgrading every listing to a library default nobody chose.
_JPEG_QUALITY = 92


def _prepare() -> tuple:
    """Import Pillow and teach it HEIC, answering (Image, ImageOps).

    Imported here rather than at module scope: every CLI invocation resolves a platform, and
    almost none of them convert a photo.
    """
    global _opener_registered
    from PIL import Image, ImageOps

    if not _opener_registered:
        import pillow_heif

        pillow_heif.register_heif_opener()
        _opener_registered = True
    return Image, ImageOps


def to_jpeg(src: Path, dest: Path, max_dim: int) -> None:
    """Write src to dest as a JPEG no larger than max_dim on its longest side.

    Raises ImageToolUnavailable — naming the file — when the conversion fails, so the caller can
    escalate something actionable rather than upload a file the marketplace will reject.
    """
    src = Path(src)
    dest = Path(dest)
    try:
        Image, ImageOps = _prepare()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as opened:
            # Image.open gives the primary frame; a HEIC's burst or depth map stays where it is.
            # Orientation is an EXIF tag rather than pixel order, so bake it in — a viewer that
            # ignores the tag shows the listing sideways.
            image = ImageOps.exif_transpose(opened) or opened
            # thumbnail only ever shrinks; a photo already inside the bound keeps its own size.
            image.thumbnail((max_dim, max_dim))
            # JPEG carries no alpha or palette, so coerce rather than fail at the last step.
            image.convert("RGB").save(dest, format="JPEG", quality=_JPEG_QUALITY)
    except Exception as exc:  # Pillow raises OSError, ValueError and its own types alike
        raise ImageToolUnavailable(f"cannot convert {src.name}: {exc}") from exc
