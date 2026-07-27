"""Photo intake and upload — the two steps between a seller's picture and a listing that shows it.

`import_photos` is the attended path: local files are sniffed as real images and copied into the
media store before anything else touches them, so the store containment gate has something to
accept (channel photos arrive already-stored, downloaded by the poller).
`carousell_ai_upload_photos` is the daemon-side bracket: convert what needs converting, mint an
upload URL per photo, PUT the bytes, and stamp the whole set at once.

The upload is all-or-nothing on purpose. A marketplace replaces an item's photo set wholesale, so a
half-stamped set does not mean "half the photos" — it means the listing ships with the wrong cover.
A partial failure therefore stamps nothing and a re-run simply uploads everything again, which is
cheap; the bug class it avoids is not.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from selly_agent import paths
from selly_agent.platform import get_platform
from selly_agent.platform.base import ImageToolUnavailable, UnsupportedPlatform
from selly_agent.rail.client import RailUnprovisioned
from selly_agent.store import MAX_PHOTOS, StoreError
from selly_agent.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_PUBLISH,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

# The longest side an uploaded photo is scaled to. The rail asks for small images and a listing
# thumbnail never needs more; a 12MP phone photo would otherwise spend seconds per upload.
MAX_UPLOAD_DIM = 1600

# What a file's first bytes must look like for us to treat it as an image. Extensions are a
# claim, not evidence — the sniff is what decides, so a mislabelled or truncated file is caught
# at intake rather than by the marketplace after a publish.
_MAGIC = (
    (b"\xff\xd8\xff", "jpeg", ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", ".png", "image/png"),
    (b"GIF87a", "gif", ".gif", "image/gif"),
    (b"GIF89a", "gif", ".gif", "image/gif"),
)
_MAGIC_READ_BYTES = 32
_JPEG = "jpeg"


def _sniff(path: Path) -> tuple:
    """(kind, suffix, content_type) for a supported image, or a ToolError naming the file."""
    try:
        head = path.open("rb").read(_MAGIC_READ_BYTES)
    except OSError as exc:
        raise ToolError(f"cannot read {path.name}: {type(exc).__name__}") from exc
    for magic, kind, suffix, content_type in _MAGIC:
        if head.startswith(magic):
            return kind, suffix, content_type
    # HEIC/HEIF are ISO-BMFF: "....ftyp<brand>" with an image brand.
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1"):
        return "heic", ".heic", "image/heic"
    raise ToolError(f"{path.name} is not a supported image (jpeg, png, gif, or heic)")


def _import_photos(ctx: ToolContext, params: dict) -> dict:
    raw_paths = params["paths"]
    if not raw_paths:
        raise ToolError("no paths given")
    if len(raw_paths) > MAX_PHOTOS:
        raise ToolError(f"at most {MAX_PHOTOS} photos at a time")

    sources = []
    for raw in raw_paths:
        src = paths.user_path(raw)
        if not src.is_file():
            raise ToolError(f"no such file: {raw}")
        _, suffix, _ = _sniff(src)
        sources.append((src, suffix))

    # Every import lands in its own directory so two files of the same name never collide, and a
    # failed import leaves nothing half-written into a directory another item is reading.
    batch = paths.media_dir() / f"import-{uuid.uuid4().hex[:12]}"
    batch.mkdir(parents=True, exist_ok=True)
    stored = []
    for index, (src, suffix) in enumerate(sources, start=1):
        dest = batch / f"{index:02d}{suffix}"
        shutil.copyfile(src, dest)
        stored.append(str(dest))
    return {"paths": stored, "count": len(stored)}


def _prepared_bytes(path: Path, workdir: Path) -> tuple:
    """(bytes, content_type) ready to upload. A JPEG goes as-is — the channel case, and the one
    that must work on any machine; anything else is converted through the platform's tool."""
    kind, _, content_type = _sniff(path)
    if kind == _JPEG:
        return path.read_bytes(), content_type
    dest = workdir / (path.stem + ".jpg")
    try:
        get_platform().to_jpeg(path, dest, MAX_UPLOAD_DIM)
    except (ImageToolUnavailable, UnsupportedPlatform) as exc:
        raise ToolError(str(exc)) from exc
    return dest.read_bytes(), "image/jpeg"


def _upload_photos(ctx: ToolContext, params: dict) -> dict:
    item_id = params["item_id"]
    item = ctx.store.get_item(item_id)
    if item is None:
        raise ToolError(f"no item with id {item_id!r}")
    photos = item["photos"]
    if not photos:
        raise ToolError("this item has no photos yet — attach some before uploading")
    if all(p.get("uploaded_url") for p in photos):
        return {"count": len(photos), "already_uploaded": True}

    if ctx.rail_factory is None:
        raise ToolError("the carousell.ai rail is not available in this session")
    try:
        rail = ctx.rail_factory()
    except RailUnprovisioned as exc:
        raise ToolError(
            "carousell.ai is not provisioned — run `selly-agent provision carousell-ai`"
        ) from exc

    workdir = paths.media_dir() / f"prepared-{uuid.uuid4().hex[:12]}"
    uploaded = []
    try:
        for photo in photos:
            data, content_type = _prepared_bytes(Path(photo["path"]), workdir)
            try:
                uploaded.append(rail.upload_photo(data, content_type))
            except RailUnprovisioned as exc:
                raise ToolError(
                    "carousell.ai is not provisioned — run `selly-agent provision carousell-ai`"
                ) from exc
            except Exception as exc:  # RailError messages are caller-safe and secret-free
                raise ToolError(f"photo upload failed: {exc}") from exc
    finally:
        # Derived copies exist only for the upload; the seller's originals stay where they are.
        shutil.rmtree(workdir, ignore_errors=True)

    try:
        ctx.store.set_photo_uploads(item_id, uploaded)
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    return {"count": len(uploaded)}


register(
    ToolSpec(
        name="import_photos",
        description="Copy local image files into the agent's media store and return their stored "
        "paths — the only way a file from outside becomes attachable to an item. Pass the stored "
        "paths to create_item/update_item as the item's photos. Photos the seller sends over the "
        "channel are already stored; use their paths directly.",
        input_schema={
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["paths"],
            "additionalProperties": False,
        },
        # Attended only: a channel photo is already in the media store by the time a pass sees it
        # (the poller puts it there), so the channel pass has nothing to import.
        handler=_import_photos,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="carousell_ai_upload_photos",
        description="Upload an item's photos to carousell.ai so a listing can show them. Call "
        "this before carousell_ai_publish_listing. All-or-nothing: either every photo uploads or "
        "none is recorded. Idempotent — an item whose photos are already uploaded is a no-op.",
        input_schema={
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
            "additionalProperties": False,
        },
        handler=_upload_photos,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL, TIER_PASS_PUBLISH}),
    )
)
