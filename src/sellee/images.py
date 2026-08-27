"""What a file's first bytes say it is.

Extensions are a claim, not evidence, and neither is a Content-Type header. The sniff decides, so a
mislabelled, truncated, or not-an-image file is caught at intake rather than by a marketplace after
a publish.

Its own module because two layers need it and neither should import the other: the photo intake
tool, and the browser layer's photo fetch. Pure and stdlib.
"""

from __future__ import annotations

from pathlib import Path

# (magic prefix, kind, suffix, content type). Ordered as cheaply-distinguishable first; HEIC is not
# a prefix match at all and is handled below.
_MAGIC = (
    (b"\xff\xd8\xff", "jpeg", ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", ".png", "image/png"),
    (b"GIF87a", "gif", ".gif", "image/gif"),
    (b"GIF89a", "gif", ".gif", "image/gif"),
)
MAGIC_READ_BYTES = 32

# Formats the rail accepts as-is. HEIC is not among them, so those are always converted.
UPLOADABLE = frozenset({"jpeg", "png", "gif"})


class UnsupportedImage(Exception):
    """The bytes are not an image we can publish. Carries a caller-safe message naming the file."""


def sniff_bytes(head: bytes) -> tuple | None:
    """(kind, suffix, content_type) for a supported image, or None — the non-raising form, for a
    caller deciding whether to keep a download rather than reporting a bad file."""
    for magic, kind, suffix, content_type in _MAGIC:
        if head.startswith(magic):
            return kind, suffix, content_type
    # HEIC/HEIF are ISO-BMFF: "....ftyp<brand>" with an image brand.
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1"):
        return "heic", ".heic", "image/heic"
    return None


def sniff_image(path: Path) -> tuple:
    """(kind, suffix, content_type) for a supported image file, or raise `UnsupportedImage`."""
    try:
        with path.open("rb") as handle:
            head = handle.read(MAGIC_READ_BYTES)
    except OSError as exc:
        raise UnsupportedImage(f"cannot read {path.name}: {type(exc).__name__}") from exc
    sniffed = sniff_bytes(head)
    if sniffed is None:
        raise UnsupportedImage(f"{path.name} is not a supported image (jpeg, png, gif, or heic)")
    return sniffed
