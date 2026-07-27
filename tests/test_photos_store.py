"""Item photos in the store: the media-store containment gate and the all-or-nothing upload
stamp."""

from __future__ import annotations

import pytest

from selly_agent import paths
from selly_agent.store import ItemNotFound, StoreError


def _media_photo(name: str = "a.jpg") -> str:
    dest = paths.media_dir() / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"\xff\xd8\xffbytes")
    return str(dest)


# --- the containment gate ----------------------------------------------------------------------


def test_photos_inside_the_media_store_are_stored_resolved(store, xdg_tmp) -> None:
    path = _media_photo()
    item = store.create_item(title="Lamp", list_price=80.0, photos=[path])
    assert item["photos"] == [{"path": path}]
    # the bare-string shorthand and the object form canonicalize the same way
    updated = store.update_item(item["id"], {"photos": [{"path": path}]})
    assert updated["photos"] == [{"path": path}]


def test_an_item_starts_with_no_photos(store, xdg_tmp) -> None:
    assert store.create_item(title="Lamp", list_price=80.0)["photos"] == []


def test_photo_outside_the_media_store_is_refused(store, xdg_tmp, tmp_path) -> None:
    outside = tmp_path / "elsewhere.jpg"
    outside.write_bytes(b"\xff\xd8\xff")
    with pytest.raises(StoreError, match="inside the media store"):
        store.create_item(title="Lamp", list_price=80.0, photos=[str(outside)])


def test_photo_traversal_out_of_the_media_store_is_refused(store, xdg_tmp) -> None:
    escape = str(paths.media_dir() / ".." / "data" / "selly.db")
    with pytest.raises(StoreError, match="inside the media store"):
        store.create_item(title="Lamp", list_price=80.0, photos=[escape])


def test_photo_symlink_out_of_the_media_store_is_refused(store, xdg_tmp, tmp_path) -> None:
    target = tmp_path / "secret.jpg"
    target.write_bytes(b"\xff\xd8\xff")
    paths.media_dir().mkdir(parents=True, exist_ok=True)
    link = paths.media_dir() / "link.jpg"
    link.symlink_to(target)
    with pytest.raises(StoreError, match="inside the media store"):
        store.create_item(title="Lamp", list_price=80.0, photos=[str(link)])


def test_photo_entry_shape_is_validated(store, xdg_tmp) -> None:
    path = _media_photo()
    with pytest.raises(StoreError, match="unknown photo field"):
        store.create_item(title="Lamp", list_price=80.0, photos=[{"path": path, "nope": 1}])
    with pytest.raises(StoreError, match="non-empty path"):
        store.create_item(title="Lamp", list_price=80.0, photos=[{"path": ""}])
    with pytest.raises(StoreError, match="must be a list"):
        store.create_item(title="Lamp", list_price=80.0, photos={"path": path})
    with pytest.raises(StoreError, match="at most"):
        store.create_item(title="Lamp", list_price=80.0, photos=[path] * 13)


def test_update_item_photos_go_through_the_same_gate(store, xdg_tmp, tmp_path) -> None:
    item = store.create_item(title="Lamp", list_price=80.0)
    outside = tmp_path / "elsewhere.jpg"
    outside.write_bytes(b"\xff\xd8\xff")
    with pytest.raises(StoreError, match="inside the media store"):
        store.update_item(item["id"], {"photos": [str(outside)]})
    assert store.get_item(item["id"])["photos"] == []


# --- the upload stamp --------------------------------------------------------------------------


def test_set_photo_uploads_stamps_the_whole_set(store, xdg_tmp) -> None:
    item = store.create_item(
        title="Lamp", list_price=80.0, photos=[_media_photo("a.jpg"), _media_photo("b.jpg")]
    )
    stamped = store.set_photo_uploads(item["id"], ["enc-a", "enc-b"])
    assert [p["uploaded_url"] for p in stamped["photos"]] == ["enc-a", "enc-b"]
    # order is display order — the first photo stays the cover
    assert stamped["photos"][0]["path"].endswith("a.jpg")


def test_set_photo_uploads_refuses_a_partial_set(store, xdg_tmp) -> None:
    item = store.create_item(
        title="Lamp", list_price=80.0, photos=[_media_photo("a.jpg"), _media_photo("b.jpg")]
    )
    with pytest.raises(StoreError, match="one uploaded url per photo"):
        store.set_photo_uploads(item["id"], ["enc-a"])
    assert all("uploaded_url" not in p for p in store.get_item(item["id"])["photos"])


def test_set_photo_uploads_on_a_missing_item(store, xdg_tmp) -> None:
    with pytest.raises(ItemNotFound):
        store.set_photo_uploads("item_nope", [])


def test_a_stamped_photo_round_trips_through_update_item(store, xdg_tmp) -> None:
    """An already-uploaded photo keeps its reference when the list is rewritten — the re-run of an
    upload must be able to see what is already stamped."""
    item = store.create_item(title="Lamp", list_price=80.0, photos=[_media_photo("a.jpg")])
    store.set_photo_uploads(item["id"], ["enc-a"])
    photos = store.get_item(item["id"])["photos"]
    assert store.update_item(item["id"], {"photos": photos})["photos"] == photos
