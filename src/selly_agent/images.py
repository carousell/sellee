"""Turning a seller's photo into something a marketplace will accept.

One portable implementation, not a per-OS one. This used to shell out to `sips` because the runtime
could carry no library, which made HEIC — what an iPhone produces by default — convertible on macOS
and nowhere else. Pillow with pillow-heif decodes it everywhere, so the capability stopped being a
property of the seller's operating system.
"""

from __future__ import annotations

from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

# Registering the plugin is what lets Image.open read a HEIC like any other format. Done once here
# rather than at each call site, because forgetting it turns into "this file is not an image".
pillow_heif.register_heif_opener()

_JPEG_QUALITY = 88


class ImageToolUnavailable(Exception):
    """A photo could not be converted. The message names the file, because the caller turns this
    into something the seller reads."""


def to_jpeg(src, dest, max_dim: int) -> None:
    """Write `src` to `dest` as a JPEG no larger than `max_dim` on its longest side.

    Only ever downscales: a photo smaller than the limit is already acceptable, and enlarging it
    would cost bytes without adding detail. EXIF orientation is applied, so a portrait phone photo
    does not arrive on its side.
    """
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(src) as opened:
            image = ImageOps.exif_transpose(opened) or opened
            image.thumbnail((max_dim, max_dim))
            _without_alpha(image).save(
                dest, "JPEG", quality=_JPEG_QUALITY, optimize=True, progressive=True
            )
    # DecompressionBombError subclasses Exception directly rather than any of the above, so a
    # photo past the bomb threshold would otherwise leave here as an unhandled handler bug
    # instead of the seller-facing "cannot convert this one" path.
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageToolUnavailable(f"cannot convert {src.name}: {exc}") from exc


def _without_alpha(image):
    """JPEG has no alpha channel, so anything carrying one is composited onto white rather than
    refused — a listing photo with transparency is a screenshot or a PNG export, not a mistake."""
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA", "PA", "P"):
        rgba = image.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.split()[-1])
        return flattened
    return image.convert("RGB")
