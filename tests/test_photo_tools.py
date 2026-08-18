"""import_photos and carousell_ai_upload_photos: intake sniffing, the all-or-nothing upload
bracket, conversion behind the platform seam, and the media the publish call carries."""

from __future__ import annotations

from pathlib import Path

import pytest

import sellee.tools  # noqa: F401  registration
from sellee import paths
from sellee.platform.base import ImageToolUnavailable
from sellee.rail.client import RailToolError, RailUnprovisioned
from sellee.tools import photos as photo_tools
from sellee.tools.registry import TIER_ATTENDED, TIER_PASS_PUBLISH, ToolError, dispatch

JPEG = b"\xff\xd8\xff\xe0" + b"jpegbytes"
PNG = b"\x89PNG\r\n\x1a\n" + b"pngbytes"
HEIC = b"\x00\x00\x00\x18ftypheic" + b"heicbytes"


class FakeRail:
    """Records every upload; can be told to fail on the Nth one to exercise a partial failure."""

    def __init__(self, *, fail_on: int | None = None):
        self.uploads: list = []
        self.listing_args: dict | None = None
        self.fail_on = fail_on

    def upload_photo(self, data: bytes, content_type: str) -> str:
        self.uploads.append((data, content_type))
        if self.fail_on is not None and len(self.uploads) == self.fail_on:
            raise RailToolError("photo upload returned HTTP 500")
        return f"enc-{len(self.uploads)}"

    def create_listing(self, args):
        self.listing_args = args
        return {"listing_id": "L1", "url": "https://www.carousell.ai/listing/1-lamp"}

    def verify_listing_url(self, url):
        return None


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _stored(name: str, data: bytes = JPEG) -> str:
    return _write(paths.media_dir() / name, data)


# --- import_photos ------------------------------------------------------------------------------


def test_import_copies_into_the_media_store(make_ctx, xdg_tmp, tmp_path) -> None:
    src = _write(tmp_path / "desktop" / "lamp.jpg", JPEG)
    result = dispatch("import_photos", {"paths": [src]}, make_ctx(TIER_ATTENDED))

    (stored,) = result["paths"]
    assert result["count"] == 1
    assert Path(stored).read_bytes() == JPEG
    assert paths.media_dir().resolve() in Path(stored).resolve().parents
    assert Path(src).exists()  # the seller's own file is copied, never moved


def test_imported_paths_are_accepted_by_the_store_gate(make_ctx, store, xdg_tmp, tmp_path) -> None:
    """The point of the tool: a path from outside is unusable until imported, and usable after."""
    src = _write(tmp_path / "lamp.jpg", JPEG)
    with pytest.raises(ToolError, match="inside the media store"):
        dispatch(
            "create_item",
            {"title": "Lamp", "list_price": 80.0, "photos": [src]},
            make_ctx(TIER_ATTENDED),
        )
    imported = dispatch("import_photos", {"paths": [src]}, make_ctx(TIER_ATTENDED))["paths"]
    item = dispatch(
        "create_item",
        {"title": "Lamp", "list_price": 80.0, "photos": imported},
        make_ctx(TIER_ATTENDED),
    )
    assert [p["path"] for p in item["photos"]] == imported


def test_import_keeps_same_named_files_apart(make_ctx, xdg_tmp, tmp_path) -> None:
    a = _write(tmp_path / "a" / "photo.jpg", JPEG)
    b = _write(tmp_path / "b" / "photo.png", PNG)
    stored = dispatch("import_photos", {"paths": [a, b]}, make_ctx(TIER_ATTENDED))["paths"]
    assert len(set(stored)) == 2
    assert stored[0].endswith(".jpg") and stored[1].endswith(".png")


def test_import_refuses_a_missing_file(make_ctx, xdg_tmp, tmp_path) -> None:
    with pytest.raises(ToolError, match="no such file"):
        dispatch("import_photos", {"paths": [str(tmp_path / "ghost.jpg")]}, make_ctx(TIER_ATTENDED))


def test_import_refuses_a_non_image_however_it_is_named(make_ctx, xdg_tmp, tmp_path) -> None:
    """The extension is a claim; the bytes decide. A .jpg full of text is caught here rather than
    by the marketplace after a publish."""
    fake = _write(tmp_path / "notes.jpg", b"just some text, honestly")
    with pytest.raises(ToolError, match="not a supported image"):
        dispatch("import_photos", {"paths": [fake]}, make_ctx(TIER_ATTENDED))


def test_import_refuses_a_whole_batch_when_one_file_is_bad(make_ctx, xdg_tmp, tmp_path) -> None:
    good = _write(tmp_path / "ok.jpg", JPEG)
    bad = _write(tmp_path / "bad.jpg", b"nope")
    before = set(paths.media_dir().rglob("*")) if paths.media_dir().exists() else set()
    with pytest.raises(ToolError, match="not a supported image"):
        dispatch("import_photos", {"paths": [good, bad]}, make_ctx(TIER_ATTENDED))
    after = set(paths.media_dir().rglob("*")) if paths.media_dir().exists() else set()
    assert after == before  # nothing half-copied


def test_import_caps_the_batch(make_ctx, xdg_tmp, tmp_path) -> None:
    many = [_write(tmp_path / f"{i}.jpg", JPEG) for i in range(13)]
    with pytest.raises(ToolError, match="at most"):
        dispatch("import_photos", {"paths": many}, make_ctx(TIER_ATTENDED))


# --- carousell_ai_upload_photos -----------------------------------------------------------------


def _item_with_photos(store, names=("a.jpg", "b.jpg"), data=JPEG):
    return store.create_item(
        title="Lamp",
        list_price=80.0,
        currency="SGD",
        photos=[_stored(n, data) for n in names],
    )


def test_upload_stamps_every_photo_in_order(make_ctx, store, xdg_tmp) -> None:
    rail = FakeRail()
    item = _item_with_photos(store)
    result = dispatch(
        "carousell_ai_upload_photos",
        {"item_id": item["id"]},
        make_ctx(TIER_ATTENDED, rail_factory=lambda: rail),
    )
    assert result == {"count": 2}
    assert [p["uploaded_url"] for p in store.get_item(item["id"])["photos"]] == ["enc-1", "enc-2"]
    assert [ct for _, ct in rail.uploads] == ["image/jpeg", "image/jpeg"]


def test_a_partial_upload_failure_stamps_nothing(make_ctx, store, xdg_tmp) -> None:
    """All-or-nothing: the second photo failing must not leave the first one stamped, or the next
    publish would ship a one-photo listing whose cover is right by luck."""
    rail = FakeRail(fail_on=2)
    item = _item_with_photos(store)
    with pytest.raises(ToolError, match="photo upload failed"):
        dispatch(
            "carousell_ai_upload_photos",
            {"item_id": item["id"]},
            make_ctx(TIER_ATTENDED, rail_factory=lambda: rail),
        )
    assert all("uploaded_url" not in p for p in store.get_item(item["id"])["photos"])


def test_upload_is_idempotent(make_ctx, store, xdg_tmp) -> None:
    rail = FakeRail()
    item = _item_with_photos(store)
    ctx = make_ctx(TIER_ATTENDED, rail_factory=lambda: rail)
    dispatch("carousell_ai_upload_photos", {"item_id": item["id"]}, ctx)
    again = dispatch("carousell_ai_upload_photos", {"item_id": item["id"]}, ctx)
    assert again == {"count": 2, "already_uploaded": True}
    assert len(rail.uploads) == 2  # never posted a second time


def test_upload_needs_photos(make_ctx, store, xdg_tmp) -> None:
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    with pytest.raises(ToolError, match="no photos yet"):
        dispatch(
            "carousell_ai_upload_photos",
            {"item_id": item["id"]},
            make_ctx(TIER_ATTENDED, rail_factory=lambda: FakeRail()),
        )


def test_upload_unprovisioned_names_the_fix(make_ctx, store, xdg_tmp) -> None:
    def factory():
        raise RailUnprovisioned("no key")

    item = _item_with_photos(store)
    with pytest.raises(ToolError, match="provision carousell-ai"):
        dispatch(
            "carousell_ai_upload_photos",
            {"item_id": item["id"]},
            make_ctx(TIER_ATTENDED, rail_factory=factory),
        )


# --- conversion behind the platform seam --------------------------------------------------------


def test_a_jpeg_uploads_untransformed(make_ctx, store, xdg_tmp, monkeypatch) -> None:
    """The channel case — Telegram already hands us JPEG — must never depend on an image tool,
    which is also why the suite runs on a non-Mac box."""

    def explode():
        raise AssertionError("the platform image tool must not be consulted for a JPEG")

    monkeypatch.setattr(photo_tools, "get_platform", explode)
    rail = FakeRail()
    item = _item_with_photos(store, names=("a.jpg",))
    dispatch(
        "carousell_ai_upload_photos",
        {"item_id": item["id"]},
        make_ctx(TIER_ATTENDED, rail_factory=lambda: rail),
    )
    assert rail.uploads == [(JPEG, "image/jpeg")]


def test_a_heic_is_converted_through_the_platform(make_ctx, store, xdg_tmp, monkeypatch) -> None:
    converted = []

    class FakePlatform:
        def to_jpeg(self, src, dest, max_dim):
            converted.append((Path(src).name, max_dim))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(JPEG)

    monkeypatch.setattr(photo_tools, "get_platform", FakePlatform)
    rail = FakeRail()
    item = _item_with_photos(store, names=("a.heic",), data=HEIC)
    dispatch(
        "carousell_ai_upload_photos",
        {"item_id": item["id"]},
        make_ctx(TIER_ATTENDED, rail_factory=lambda: rail),
    )
    assert converted == [("a.heic", photo_tools.MAX_UPLOAD_DIM)]
    assert rail.uploads == [(JPEG, "image/jpeg")]


def test_a_missing_image_tool_names_the_file_and_stamps_nothing(
    make_ctx, store, xdg_tmp, monkeypatch
) -> None:
    class NoTool:
        def to_jpeg(self, src, dest, max_dim):
            raise ImageToolUnavailable(f"cannot convert {Path(src).name}: truncated file")

    monkeypatch.setattr(photo_tools, "get_platform", NoTool)
    item = _item_with_photos(store, names=("a.heic",), data=HEIC)
    with pytest.raises(ToolError, match="cannot convert a.heic"):
        dispatch(
            "carousell_ai_upload_photos",
            {"item_id": item["id"]},
            make_ctx(TIER_ATTENDED, rail_factory=lambda: FakeRail()),
        )
    assert all("uploaded_url" not in p for p in store.get_item(item["id"])["photos"])


def test_conversion_leaves_no_derived_copies_behind(make_ctx, store, xdg_tmp, monkeypatch) -> None:
    class FakePlatform:
        def to_jpeg(self, src, dest, max_dim):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(JPEG)

    monkeypatch.setattr(photo_tools, "get_platform", FakePlatform)
    item = _item_with_photos(store, names=("a.heic",), data=HEIC)
    dispatch(
        "carousell_ai_upload_photos",
        {"item_id": item["id"]},
        make_ctx(TIER_ATTENDED, rail_factory=lambda: FakeRail()),
    )
    assert not list(paths.media_dir().glob("prepared-*"))


# --- what publish sends -------------------------------------------------------------------------


def test_publish_attaches_uploaded_media_in_display_order(make_ctx, store, xdg_tmp) -> None:
    rail = FakeRail()
    item = _item_with_photos(store)
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    dispatch("carousell_ai_upload_photos", {"item_id": item["id"]}, ctx)
    dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert rail.listing_args["media"] == {
        "urls": [{"url": "enc-1", "type": 1}, {"url": "enc-2", "type": 1}]
    }


def test_publish_without_photos_sends_no_media_key(make_ctx, store, xdg_tmp) -> None:
    rail = FakeRail()
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    dispatch(
        "carousell_ai_publish_listing",
        {"item_id": item["id"]},
        make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail),
    )
    assert "media" not in rail.listing_args


def test_publish_skips_a_photo_that_was_never_uploaded(make_ctx, store, xdg_tmp) -> None:
    """A local path means nothing to the rail, so it is left out rather than sent as a URL."""
    rail = FakeRail()
    item = _item_with_photos(store)
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    dispatch("carousell_ai_upload_photos", {"item_id": item["id"]}, ctx)
    photos = store.get_item(item["id"])["photos"]
    store.update_item(item["id"], {"photos": [photos[0], {"path": _stored("c.jpg")}]})

    dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)
    assert rail.listing_args["media"] == {"urls": [{"url": "enc-1", "type": 1}]}
