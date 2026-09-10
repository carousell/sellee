"""An item's title is public text, so it is normalised at the boundary that stores it.

A title travels straight onto every marketplace the item is listed to. Whatever composes it — a
model reading a photo in a channel pass, an adoption reading a marketplace's own page, a person in
an attended session — is upstream of the store, and none of them are a place to enforce this once.

Found by walking the product: a channel pass created "DJI Mic Mini Wireless Transmitter (single,
w/ case &amp; pouch)". Nothing in the codebase HTML-escapes anything and Telegram outbound is plain
text, so the model simply wrote the entity — and it would have gone up on a Craigslist posting
reading "case &amp; pouch" to every buyer who saw it.
"""

from __future__ import annotations

import pytest

from sellee.store import StoreError


def test_an_html_entity_in_a_title_is_decoded(store) -> None:
    item = store.create_item(
        title="DJI Mic Mini (single, w/ case &amp; pouch)", list_price=22.0, currency="USD"
    )
    assert item["title"] == "DJI Mic Mini (single, w/ case & pouch)"


@pytest.mark.parametrize(
    "written,expected",
    [
        ("Sony &quot;pro&quot; lens", 'Sony "pro" lens'),
        ("Chair &lt;used&gt;", "Chair <used>"),
        ("Nuts &amp; bolts &amp; screws", "Nuts & bolts & screws"),
        ("caf&eacute; table", "café table"),
        # Already-clean titles are untouched, including a bare ampersand.
        ("Nuts & bolts", "Nuts & bolts"),
        ("100% wool", "100% wool"),
    ],
)
def test_entities_are_decoded_and_clean_titles_left_alone(store, written, expected) -> None:
    item = store.create_item(title=written, list_price=10.0, currency="USD")
    assert item["title"] == expected


def test_whitespace_is_tidied_but_the_title_is_not_rewritten(store) -> None:
    """Normalising is not editing: the seller's own wording, capitalisation and punctuation are
    theirs. Only the encoding and stray edge whitespace are ours."""
    item = store.create_item(title="  Vintage LAMP, brass  ", list_price=10.0, currency="USD")
    assert item["title"] == "Vintage LAMP, brass"


def test_a_title_that_is_only_an_entity_is_still_refused(store) -> None:
    """`&nbsp;` decodes to whitespace, so a title that looked non-empty becomes empty — and an item
    with no title cannot be listed anywhere."""
    with pytest.raises(StoreError):
        store.create_item(title="&nbsp;", list_price=10.0, currency="USD")


def test_a_title_written_by_an_update_is_normalised_too(store) -> None:
    """`create_item` was not the only door. The channel pass that produced the live `&amp;` called
    `update_item` twice after creating the item, so normalising only creation left the entity in
    place — and the notice telling the seller the listing had stalled printed it back at them.
    """
    item = store.create_item(title="Clean title", list_price=10.0, currency="USD")
    updated = store.update_item(item["id"], {"title": "Mic (w/ case &amp; pouch)"})
    assert updated["title"] == "Mic (w/ case & pouch)"


def test_an_update_that_empties_the_title_is_refused(store) -> None:
    item = store.create_item(title="Clean title", list_price=10.0, currency="USD")
    with pytest.raises(StoreError):
        store.update_item(item["id"], {"title": "&nbsp;"})


def test_an_update_that_does_not_touch_the_title_is_unaffected(store) -> None:
    item = store.create_item(title="Nuts & bolts", list_price=10.0, currency="USD")
    updated = store.update_item(item["id"], {"list_price": 12.0})
    assert updated["title"] == "Nuts & bolts"
    assert updated["list_price"] == 12.0
