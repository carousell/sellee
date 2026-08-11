"""The photo converter: one implementation, every platform.

The pipeline needs exactly one transform — take whatever a phone or a marketplace hands us and
produce a JPEG the rail will accept, no larger than a bound. That used to be the OS's job, which
meant `sips` on macOS and ImageMagick in the image, and no answer at all on a host Linux. Pillow
does it identically everywhere, so the transform stopped being OS-specific and moved here; what
stays behind the platform seam is only what genuinely differs between operating systems.

HEIC is the reason a decoder is bundled at all: an iPhone photo arrives in it, no marketplace
takes it, and Pillow does not read it without pillow-heif.
"""

from __future__ import annotations

from pathlib import Path

from selly_agent.platform.base import ImageToolUnavailable

_opener_registered = False


def _prepare() -> tuple:
    """Import Pillow and teach it HEIC, answering (Image, ImageOps).

    Imported here rather than at module scope because Pillow is a heavy import and this is a rare
    path: every CLI invocation resolves a platform, and almost none of them convert a photo.
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
            # A phone's HEIC can carry a depth map or a burst alongside the picture; Image.open
            # gives the primary image and the rest is left where it is.
            #
            # Orientation is an EXIF tag rather than pixel order, and a JPEG written without
            # applying it shows sideways wherever the tag is ignored — so it is baked in here.
            image = ImageOps.exif_transpose(opened) or opened
            # thumbnail only ever shrinks: a photo already inside the bound is re-encoded at its
            # own size, never blown up to meet it.
            image.thumbnail((max_dim, max_dim))
            # JPEG has no alpha and no palette; converting first means a PNG with transparency
            # writes rather than raising at the last step.
            image.convert("RGB").save(dest, format="JPEG")
    except Exception as exc:  # Pillow raises OSError, ValueError and its own types alike
        raise ImageToolUnavailable(f"cannot convert {src.name}: {exc}") from exc
