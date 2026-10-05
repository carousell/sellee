"""Editing a live Craigslist post: the revise lane drives the post's manage page, replaces what
changed, reads it back on the preview, publishes, and confirms on the manage page."""

from __future__ import annotations

import contextlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.conftest import seed_setting

from sellee import revise
from sellee.browser import editor
from sellee.browser import markets as market_adapters
from sellee.browser.markets import craigslist
from sellee.config import Config

_TOKEN = "9TXkjafxYMDS5VaPLGdRyk"
_POST = f"https://www.craigslist.org/view/d/samsung-buds3/{_TOKEN}"
_MANAGE = f"https://post.craigslist.org/manage/{_TOKEN}"
_BOXES = {craigslist.TITLE: "title", craigslist.PRICE: "price", craigslist.BODY: "description"}


class FakePost:
    """One live post: its manage page, and an edit that changes a draft until publish."""

    def __init__(self, *, images=3, sticky_box=None, publishes=True, uploads=True, post_url=_POST):
        self.post = {"title": "Samsung Buds3", "price": "80", "description": "Barely used."}
        self.post_images = images
        self.draft: dict = {}
        self.images = 0
        self.step = ""
        self.sticky_box = sticky_box
        self.publishes = publishes
        self.uploads = uploads
        self.post_url = post_url
        self.focus: str | None = None
        self.selected = False
        self.navigated: list = []
        self.paced = 0
        self.clicks = 0
        self.marked_delete = False
        self.published = 0
        self.signed_in = True

    @contextlib.contextmanager
    def exclusive(self):
        yield

    def navigate_visible(self, url: str) -> None:
        self.navigate(url)

    def navigate(self, url: str) -> None:
        self.navigated.append(url)
        self.step = ""

    def pace(self, url: str) -> None:
        self.paced += 1

    def evaluate(self, function: str, **_):
        if function == craigslist.STEP_JS:
            return {"step": self.step, "logged_in": self.signed_in, "images": self.images}
        if function == craigslist.EDIT_READBACK_JS:
            return {**self.draft, "zip": "94103", "chat_on": False}
        if function == craigslist.PREVIEW_TEXT_JS:
            d = self.draft
            return {"text": f"{d['title']} - ${d['price']} (Downtown)\n{d['description']}"}
        if function == craigslist.PUBLISH_MARK_JS:
            return {"marked": self.step == "preview"}
        if function == craigslist.DELETE_IMAGE_MARK_JS:
            self.marked_delete = self.images > 0
            return {"marked": self.marked_delete}
        if function == craigslist.MANAGED_POST_JS:
            p = self.post
            text = f"{p['title']} - ${p['price']} (Downtown)\n{p['description']}\npost id: 1"
            return {"url": self.post_url, "post_id": "1", "text": text}
        raise AssertionError(f"unexpected script: {function[:60]}")

    def click(self, target: str, element: str) -> None:
        self.clicks += 1
        assert "discard" not in target, "cancel edit must never be pressed"
        if target == craigslist.EDIT_TEXT:
            self.draft, self.step = dict(self.post), "edit"
        elif target == craigslist.EDIT_IMAGES:
            self.draft, self.images, self.step = dict(self.post), self.post_images, "editimage"
        elif target in _BOXES:
            self.focus, self.selected = target, False
        elif target == craigslist.CONTINUE and self.step == "edit":
            self.step = "preview"
        elif target == craigslist.DONE_WITH_IMAGES:
            self.step = "preview"
        elif target == craigslist.ADD_IMAGES:
            pass
        elif target == craigslist.DELETE_IMAGE:
            assert self.marked_delete
            self.images -= 1
        elif target == craigslist.PUBLISH:
            assert self.step == "preview"
            self.published += 1
            if self.publishes:
                self.post = dict(self.draft)
                self.post_images = self.images
            self.step = "confirmed"
        else:
            raise AssertionError(f"unexpected click on {target} at {self.step!r}")

    def call_tool(self, name: str, arguments: dict):
        if name == "browser_press_key":
            if arguments["key"] == "Backspace" and self.selected and self.focus:
                self.draft[_BOXES[self.focus]] = ""
            self.selected = arguments["key"] != "Backspace"
        elif name == "browser_file_upload" and self.uploads:
            self.images += len(arguments["paths"])
        return ""

    def type_humanly(self, target: str, element: str, text: str) -> None:
        field = _BOXES[target]
        if field == self.sticky_box:
            return
        self.draft[field] = (self.draft.get(field) or "") + text


def _item(**more) -> dict:
    return {
        "id": "i1",
        "title": "Samsung Buds3 Pro",
        "list_price": 65.0,
        "description": "Barely used.",
        **more,
    }


def _revise(post, item, changed, listing_url=_POST):
    return editor.revise(
        post,
        market_adapters.get_adapter("craigslist"),
        item,
        listing_url=listing_url,
        changed=changed,
        sleep=lambda _s: None,
    )


# --- the manage URL ------------------------------------------------------------------------------

_URLS = st.one_of(
    st.builds(
        "https://www.craigslist.org/view/d/{}/{}".format,
        st.from_regex(r"[a-z0-9]{1,8}(-[a-z0-9]{1,8}){0,3}", fullmatch=True),
        st.from_regex(r"[A-Za-z0-9]{8,24}", fullmatch=True),
    ),
    st.text(max_size=60),
    st.sampled_from(
        [
            "https://sfbay.craigslist.org/sfc/sop/d/x/7978162581.html",
            _POST + "/../../manage/OTHER",
            "https://evil.example/view/d/x/" + _TOKEN,
        ]
    ),
)


@settings(max_examples=150, deadline=None)
@given(listing_url=_URLS)
def test_an_edit_opens_only_the_manage_page_of_the_recorded_post(listing_url) -> None:
    post = FakePost(post_url=listing_url)

    token = listing_url.rsplit("/", 1)[-1]

    try:
        _revise(post, _item(), ["title"], listing_url)
    except editor.ReviseNotAttempted:
        pass

    assert set(post.navigated) <= {f"https://post.craigslist.org/manage/{token}"}
    if not post.navigated:
        return
    assert listing_url.startswith("https://www.craigslist.org/view/d/")


# --- the edit ------------------------------------------------------------------------------------


def test_a_title_and_price_edit_replaces_both_and_is_confirmed_on_the_manage_page() -> None:
    post = FakePost()

    outcome = _revise(post, _item(), ["title", "list_price"])

    assert outcome.verified
    assert post.post["title"] == "Samsung Buds3 Pro" and post.post["price"] == "65"
    assert post.published == 1
    # Edit this Posting, continue and publish load pages; the two box clicks do not.
    assert (post.clicks, post.paced) == (5, 3)


def test_a_description_edit_lands() -> None:
    post = FakePost()

    item = _item(title="Samsung Buds3", list_price=80.0, description="Barely used, with case.")

    outcome = _revise(post, item, ["description"])

    assert outcome.verified and post.post["description"] == "Barely used, with case."


def test_new_photos_replace_every_old_one(tmp_path, monkeypatch) -> None:
    photos = []
    for name in ("a.jpg", "b.jpg"):
        path = tmp_path / name
        path.write_bytes(b"jpg")
        photos.append({"path": str(path)})
    monkeypatch.setattr("sellee.paths.publish_staging_dir", lambda: tmp_path / "staged")
    post = FakePost(images=3)

    outcome = _revise(post, _item(photos=photos), ["photos"])

    assert outcome.verified
    assert post.post_images == 2


def test_a_box_that_did_not_take_the_change_publishes_nothing() -> None:
    post = FakePost(sticky_box="price")

    with pytest.raises(editor.ReviseNotAttempted):
        _revise(post, _item(), ["list_price"])

    assert post.published == 0


def test_a_publish_that_did_not_land_reports_the_fields_still_old() -> None:
    outcome = _revise(FakePost(publishes=False), _item(), ["title", "list_price"])

    assert not outcome.verified
    assert set(outcome.mismatched) == {"title", "list_price"}


def test_craigslist_can_edit_title_price_description_and_photos() -> None:
    assert editor.can_edit_fields("craigslist", ["title", "list_price", "description", "photos"])


def _photos(tmp_path, monkeypatch, count=2) -> list:
    monkeypatch.setattr("sellee.paths.publish_staging_dir", lambda: tmp_path / "staged")
    photos = []
    for index in range(count):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(b"jpg")
        photos.append({"path": str(path)})
    return photos


def test_a_combined_edit_reports_a_text_change_that_did_not_land(tmp_path, monkeypatch) -> None:
    item = _item(photos=_photos(tmp_path, monkeypatch))

    outcome = _revise(FakePost(publishes=False), item, ["title", "photos"])

    assert not outcome.verified and "title" in outcome.mismatched


def test_a_photo_half_that_fails_after_the_text_published_is_unverified() -> None:
    post = FakePost()

    with pytest.raises(editor.ReviseUnverified):
        _revise(post, _item(photos=[]), ["title", "photos"])

    assert post.published == 1


def test_a_photo_swap_cut_off_after_removing_images_is_unverified(tmp_path, monkeypatch):
    monkeypatch.setattr("sellee.browser.craigslist_publisher.IMAGE_WAIT_SEC", 1.0)
    post = FakePost(images=3, uploads=False)

    with pytest.raises(editor.ReviseUnverified):
        _revise(post, _item(photos=_photos(tmp_path, monkeypatch)), ["photos"])

    assert post.published == 0


def test_a_photo_edit_that_did_not_land_is_reported(tmp_path, monkeypatch) -> None:
    post = FakePost(images=3, publishes=False)

    outcome = _revise(post, _item(photos=_photos(tmp_path, monkeypatch)), ["photos"])

    assert not outcome.verified and outcome.mismatched == ("photos",)


def test_a_photo_that_cannot_be_read_stops_the_swap_before_anything_is_removed(
    tmp_path, monkeypatch
) -> None:
    photos = _photos(tmp_path, monkeypatch) + [{"path": str(tmp_path / "missing.jpg")}]
    post = FakePost(images=3)

    with pytest.raises(editor.ReviseNotAttempted) as raised:
        _revise(post, _item(photos=photos), ["photos"])

    assert raised.value.retryable
    assert post.navigated == [] and post.post_images == 3


def test_a_manage_page_naming_another_post_is_unverified() -> None:
    other = "https://www.craigslist.org/view/d/other-thing/AAAAbbbbCCCC"

    with pytest.raises(editor.ReviseUnverified):
        _revise(FakePost(post_url=other), _item(), ["title"])


def test_an_empty_change_is_not_attempted() -> None:
    post = FakePost()

    with pytest.raises(editor.ReviseNotAttempted):
        _revise(post, _item(), [])

    assert post.navigated == []


# --- through the revise lane ---------------------------------------------------------------------


@pytest.fixture
def listed(store, monkeypatch):
    monkeypatch.setattr("sellee.browser.formfill.sleep", lambda _seconds: None)
    seed_setting(store, "connected_markets", ["craigslist"])
    made = store.create_item(title="Samsung Buds3 Pro", list_price=65.0, currency="USD")
    store.record_listing_url(made["id"], "craigslist", _POST)
    store.request_craigslist_signup()
    store.set_craigslist_awaiting_activation()
    store.activate_craigslist_account("ready")
    return made


def _deps(store, bus, post):
    return revise.ReviseDeps(store=store, bus=bus, config=Config(), browser_factory=lambda: post)


def test_an_edit_to_a_live_craigslist_post_is_driven_to_done(store, bus, listed) -> None:
    post = FakePost()
    rev = store.queue_listing_revision(listed["id"], "craigslist", ["title", "list_price"])

    assert revise.run_next(_deps(store, bus, post)) == rev

    assert store.get_listing_revision(rev)["status"] == "done"
    assert post.post["price"] == "65"


def test_an_item_with_no_craigslist_post_settles_failed_and_drives_nothing(
    store, bus, listed
) -> None:
    rev = store.queue_listing_revision(listed["id"], "craigslist", ["title"])
    with store._db.transaction() as conn:
        conn.execute("UPDATE items SET listing_urls = '{}' WHERE id = ?", (listed["id"],))
    post = FakePost()

    revise.run_next(_deps(store, bus, post))

    row = store.get_listing_revision(rev)
    assert row["status"] == "failed" and "no longer listed" in row["last_error"]
    assert post.navigated == []


def test_a_craigslist_edit_reserves_its_own_page_budget() -> None:
    assert revise._edit_loads("craigslist") == craigslist.EDIT_LOADS
    assert revise._edit_loads("fb") == revise.EDIT_LOADS


def test_a_signed_out_edit_signs_back_in_and_waits_unclaimed(store, bus, listed) -> None:
    post = FakePost()
    post.signed_in = False
    rev = store.queue_listing_revision(listed["id"], "craigslist", ["title"])

    revise.run_next(_deps(store, bus, post))

    assert store.craigslist_account()["state"] == "awaiting_login_link"
    row = store.get_listing_revision(rev)
    assert row["status"] == "pending" and post.published == 0
    claimed = row["attempts"]

    # Held while signing back in: the next tick claims nothing and opens nothing.
    post.navigated.clear()
    assert revise.run_next(_deps(store, bus, post)) is None
    assert store.get_listing_revision(rev)["attempts"] == claimed and post.navigated == []
