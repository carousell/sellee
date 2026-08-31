"""Publishing a listing by driving the form, rather than by asking a model to.

A marketplace publish has always been a recipe skill: a pass reads the steps in prose and drives
Chrome itself. That works, and it is what Carousell uses. It also costs about $1.54 a listing in
model turns to do something entirely deterministic — fill six fields, pick two dropdowns, press
Publish — and it is only as repeatable as a model reading prose.

So this is the other way, and the two coexist. `supported_markets` asks whether a market has a
recipe **or** publish selectors, which keeps capability derived from the code that implements it;
a market with neither cannot be published to and says so.

The shape is `browser/sink.py`'s, because a publish has the same problem a send does: it commits
something the seller cannot take back, and the moment of commit is the one place we cannot see. So
the same bracket applies, and it is the reason for the two exception types below rather than one —

  * everything before the commit fails as `PublishNotAttempted`: nothing was created, and the
    caller may try again;
  * everything from the commit onward fails as `PublishUnverified`: a listing may exist, and
    re-driving it would give the seller two of them.

Nothing here decides *what* to say. Title, price, description and photographs arrive as an item;
the driver's whole job is to get them into the form intact and to prove they arrived before
pressing anything.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from sellee import paths
from sellee.browser import markets as market_adapters
from sellee.browser import reconcile
from sellee.browser.client import BrowserError

log = logging.getLogger(__name__)

# The fields that must be on the form before anything is typed. The two text inputs are
# indistinguishable except by the label beside them and they sit one above the other, so a form we
# only partly recognise is one that could put the price in the title.
REQUIRED_FIELDS = ("title", "price", "next")

# How long the form is given to settle between steps, in seconds. A dropdown fetches its options.
STEP_SETTLE_SEC = 2.0
COMMIT_SETTLE_SEC = 6.0


class PublishNotAttempted(BrowserError):
    """Nothing was created. Retryable, and the caller should retry."""


class PublishUnverified(BrowserError):
    """A listing may exist. Never re-driven — the seller would end up with two."""


@dataclass(frozen=True)
class PublishOutcome:
    """What the drive produced: the listing if it could be shown, and how sure we are."""

    listing_id: str | None
    url: str
    verified: bool
    reason: str = ""


def publish(
    client, adapter, item: dict, *, create_url: str, photos=(), listings_url=None, sleep=None
) -> PublishOutcome:
    """Fill this market's create form from an item and publish it.

    Answers a `PublishOutcome`. Raises `PublishNotAttempted` when nothing was created and
    `PublishUnverified` when something may have been — never a bare `BrowserError`, because the
    caller's decision to retry turns entirely on which of those two it is.
    """
    if not adapter.publish_fields_js:
        raise PublishNotAttempted(f"{adapter.market} has no publish selectors")
    pause = sleep or _sleep

    client.navigate_visible(create_url)
    pause(STEP_SETTLE_SEC)
    _open_all_fields(client, adapter, pause)
    found = client.evaluate(adapter.publish_fields_js) or {}
    _refuse_unless_ready(adapter.market, found)

    if photos:
        _attach(client, adapter, photos, found, pause)
        pause(STEP_SETTLE_SEC)
    _fill_text(client, adapter, item, found)
    _choose(client, adapter, "condition", _condition_for(item), found, pause)
    _choose(client, adapter, "category", adapter.publish_default_category, found, pause)
    _refuse_paid_promotion(client, adapter)
    _verify_form(client, adapter, item)

    # Everything past here may have created a listing.
    return _commit(client, adapter, item, listings_url, pause)


def _open_all_fields(client, adapter, pause) -> None:
    """Expand whatever the form keeps collapsed, so the marking pass can see every field.

    Facebook hides the description behind "More details". Best-effort: a form that has no such
    section, or has already been expanded, simply has nothing to click, and the marking pass that
    follows is what decides whether the form is usable.
    """
    found = client.evaluate(adapter.publish_fields_js) or {}
    if "more" not in (found.get("marked") or []):
        return
    try:
        client.call_tool(
            "browser_click",
            {"target": adapter.publish_target("more"), "element": "the rest of the listing fields"},
        )
        pause(STEP_SETTLE_SEC)
    except BrowserError:
        log.debug("could not expand the %s create form", adapter.market, exc_info=True)


def _refuse_unless_ready(market: str, found: dict) -> None:
    missing = [step for step in REQUIRED_FIELDS if step not in (found.get("marked") or [])]
    if missing:
        raise PublishNotAttempted(
            f"the {market} create form is missing {missing} — nothing was filled in"
        )


def _attach(client, adapter, photos, found: dict, pause) -> None:
    """Hand the item's photographs to the form.

    Two steps, because a file cannot simply be handed to the input: the browser server only accepts
    an upload while a file chooser is actually open, so the control that opens one is pressed first
    and the paths follow. A marketplace that requires a photograph — Facebook does, and leaves Next
    greyed out until it has one — makes this the step everything else waits on.
    """
    if "add_photos" in (found.get("marked") or []):
        try:
            client.call_tool(
                "browser_click",
                {"target": adapter.publish_target("add_photos"), "element": "Add photos"},
            )
            pause(STEP_SETTLE_SEC)
        except BrowserError as exc:
            raise PublishNotAttempted(f"the photo chooser would not open: {exc}") from exc
    try:
        client.call_tool("browser_file_upload", {"paths": [str(path) for path in photos]})
    except BrowserError as exc:
        raise PublishNotAttempted(f"the photographs would not attach: {exc}") from exc


def _fill_text(client, adapter, item: dict, found: dict) -> None:
    """Type the fields that are text. Never `value =`: the form listens for real input, and a value
    set from script leaves React holding the old one — which publishes an empty listing."""
    for step, text in _text_fields(item):
        if step not in (found.get("marked") or []) or not text:
            continue
        try:
            client.call_tool(
                "browser_type",
                {
                    "target": adapter.publish_target(step),
                    "element": f"the {step} field",
                    "text": str(text),
                    "submit": False,
                },
            )
        except BrowserError as exc:
            raise PublishNotAttempted(f"could not fill {step}: {exc}") from exc


def _text_fields(item: dict) -> list:
    price = item.get("list_price")
    return [
        ("title", item.get("title") or ""),
        # Typed without a currency symbol or separators: the field formats what it is given, and a
        # grouped "1,299" has been read as 1 by more than one marketplace form.
        (
            "price",
            f"{price:.0f}" if isinstance(price, (int, float)) and price == int(price) else price,
        ),
        ("description", item.get("description") or ""),
    ]


def _choose(client, adapter, step: str, wanted: str, found: dict, pause) -> None:
    """Open one dropdown and pick an option by name.

    A dropdown we cannot satisfy is fatal *before* the commit rather than skipped: Facebook requires
    both of these, so carrying on would press Publish against a form that refuses, and the failure
    would arrive with the listing half-made and no way to tell.
    """
    if step not in (found.get("marked") or []) or not wanted:
        return
    try:
        client.call_tool(
            "browser_click",
            {"target": adapter.publish_target(step), "element": f"the {step} dropdown"},
        )
        pause(STEP_SETTLE_SEC)
        answer = client.evaluate(adapter.publish_options_js(wanted)) or {}
        if not answer.get("chosen"):
            raise PublishNotAttempted(
                f"{adapter.market} offers no {step} called {wanted!r} "
                f"(it offers {(answer.get('options') or [])[:8]})"
            )
        client.call_tool(
            "browser_click",
            {"target": adapter.publish_target("option"), "element": f"the {step}"},
        )
        pause(STEP_SETTLE_SEC)
    except BrowserError as exc:
        if isinstance(exc, PublishNotAttempted):
            raise
        raise PublishNotAttempted(f"could not choose a {step}: {exc}") from exc


def _refuse_paid_promotion(client, adapter) -> None:
    """Never publish with a paid boost switched on.

    It spends the seller's money on something they did not ask for, and it is one stray click from
    being on. Read rather than assumed: the switch ships off, and "it defaults to off" is exactly
    the kind of belief that stops being true in a release nobody told us about.
    """
    found = client.evaluate(adapter.publish_fields_js) or {}
    if not found.get("boost_on"):
        return
    try:
        client.call_tool(
            "browser_click",
            {"target": adapter.publish_target("boost"), "element": "the paid boost switch"},
        )
    except BrowserError as exc:
        raise PublishNotAttempted(f"the paid boost was on and would not turn off: {exc}") from exc
    if (client.evaluate(adapter.publish_fields_js) or {}).get("boost_on"):
        raise PublishNotAttempted("the paid boost is still on — refusing to publish")


def _verify_form(client, adapter, item: dict) -> None:
    """Read the form back before pressing anything.

    The last moment at which a mistake is free. A field that silently truncated, or refused what it
    was given, becomes a live listing the seller has to find and fix — so what is on the form is
    compared with what we meant to put there, and a mismatch fails while nothing exists yet.
    """
    if not adapter.publish_readback_js:
        return
    seen = client.evaluate(adapter.publish_readback_js) or {}
    title = (item.get("title") or "").strip()
    got = (seen.get("title") or "").strip()
    if title and got != title:
        raise PublishNotAttempted(f"the form shows the title as {got!r}, not {title!r}")
    price = item.get("list_price")
    digits = "".join(ch for ch in str(seen.get("price") or "") if ch.isdigit())
    if isinstance(price, (int, float)) and digits and int(digits) != int(price):
        raise PublishNotAttempted(f"the form shows the price as {seen.get('price')!r}, not {price}")


def _commit(client, adapter, item: dict, listings_url, pause) -> PublishOutcome:
    """Press through the form's own steps, and find out what was made.

    Everything in here is past the point of no return, so every failure is `PublishUnverified`: a
    Next that lands and a Publish that does not still leaves a draft, and a Publish whose result we
    could not read may well be a live listing. Re-driving either would give the seller two.
    """
    # The last check on the safe side of the line, and it belongs here rather than with the other
    # refusals because it is about the button we are about to press. A marketplace greys Next out
    # until it has everything it requires, and clicking a disabled button submits nothing — so
    # treating that failure as "the publish may have gone through" would retire the item forever
    # over a missing photograph. Verified live: Facebook disables Next until a photo is attached.
    #
    # Read fresh: the caller's `found` was taken before a single field was filled, when Next is
    # disabled on every form there is.
    ready = client.evaluate(adapter.publish_fields_js) or {}
    if ready.get("next_enabled") is False:
        raise PublishNotAttempted(
            f"the {adapter.market} form will not accept this listing yet — it still wants "
            "something, and nothing was submitted"
        )
    try:
        client.call_tool(
            "browser_click", {"target": adapter.publish_target("next"), "element": "Next"}
        )
        pause(COMMIT_SETTLE_SEC)
        after = client.evaluate(adapter.publish_fields_js) or {}
        if "publish" not in (after.get("marked") or []):
            raise PublishUnverified("the form moved on but offered no Publish button")
        client.call_tool(
            "browser_click", {"target": adapter.publish_target("publish"), "element": "Publish"}
        )
        pause(COMMIT_SETTLE_SEC)
    except PublishUnverified:
        raise
    except BrowserError as exc:
        raise PublishUnverified(f"the publish may have gone through: {exc}") from exc

    result = client.evaluate(adapter.publish_result_js) or {}
    listing_id = result.get("listing_id")
    if listing_id:
        return PublishOutcome(
            listing_id=str(listing_id), url=str(result.get("url") or ""), verified=True
        )

    # The page we land on may not name the listing at all — Facebook redirects to its selling page,
    # whose cards carry no id — so the seller's own listings are asked instead. Verified live: the
    # publish worked and this was the only thing standing between "done" and "we cannot tell".
    found = _confirm_by_title(client, adapter, item, listings_url, pause)
    if found is not None:
        return found
    # Not an error: a publish whose listing we could not name is reported as unverified and left
    # for a human, rather than retried into a duplicate.
    return PublishOutcome(
        listing_id=None,
        url=str(result.get("url") or ""),
        verified=False,
        reason="published, but the new listing could not be identified afterwards",
    )


def _confirm_by_title(client, adapter, item: dict, listings_url, pause) -> PublishOutcome | None:
    """Find the listing we just made among the seller's own, by title.

    Only ever used to *confirm* — never to decide whether to publish — which is what makes matching
    on a title acceptable here. The listing has already been created either way; the question is
    only whether we can name it, and the alternative to a title is a human going to look.

    Ambiguity abstains. Two live listings sharing this title means the seller had one already, and
    claiming the wrong id would record a URL that points at the older listing, so buyers on the new
    one would never be joined to this item.
    """
    if not (listings_url and adapter.my_listings_js):
        return None
    try:
        client.navigate_visible(listings_url)
        pause(STEP_SETTLE_SEC)
        if adapter.my_listings_entry_js:
            answer = client.evaluate(adapter.my_listings_entry_js) or {}
            if not answer.get("url"):
                return None
            client.navigate_visible(urljoin(listings_url, str(answer["url"])))
            pause(STEP_SETTLE_SEC)
        listings = (client.evaluate(adapter.my_listings_js) or {}).get("listings") or []
    except BrowserError:
        log.debug(
            "could not confirm the %s publish from the listings page", adapter.market, exc_info=True
        )
        return None

    wanted = reconcile.normalize(item.get("title") or "")
    matches = [row for row in listings if reconcile.normalize(row.get("title") or "") == wanted]
    if len(matches) != 1:
        return None
    row = matches[0]
    return PublishOutcome(
        listing_id=str(row.get("listing_id") or ""), url=str(row.get("url") or ""), verified=True
    )


def _condition_for(item: dict) -> str:
    """This market's own word for the item's condition.

    Conditions are free text on an item — they come from whatever another marketplace called it —
    and Facebook offers exactly four. Where the two do not meet, this understates rather than
    overstates: telling a buyer something is more used than it is costs the seller a little, and the
    reverse is a lie told on their behalf.
    """
    said = (item.get("condition") or "").strip().lower()
    if "like new" in said or "open box" in said:
        return "Used - Like New"
    if said.startswith("new") or said == "brand new":
        return "New"
    if "fair" in said or "heavily" in said or "well used" in said or "poor" in said:
        return "Used - Fair"
    return "Used - Good"


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def stage_photos(item_id: str, photos) -> list:
    """Copy an item's photographs somewhere the browser server will actually read them.

    The server only opens files under its own allowed roots, so the media store is out of reach —
    a publish that handed over a path from there was refused with "outside allowed roots", which is
    fatal on a marketplace that requires a picture. Copied rather than moved: the media store is the
    item's own record of its photographs and nothing here may disturb it.

    Answers the staged paths, in order, skipping any that could not be copied — a listing with three
    of four photographs is worth publishing, where one with none may not even be accepted.
    """
    staged: list = []
    if not photos:
        return staged
    target = paths.publish_staging_dir() / str(item_id)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("could not make a staging directory for %s", item_id, exc_info=True)
        return staged
    for index, source in enumerate(photos, start=1):
        candidate = Path(str(source))
        destination = target / f"{index:02d}{candidate.suffix or '.jpg'}"
        try:
            shutil.copyfile(candidate, destination)
        except OSError:
            log.warning("could not stage %s for publishing", candidate, exc_info=True)
            continue
        staged.append(str(destination))
    return staged


def clear_staged(item_id: str) -> None:
    """Drop what `stage_photos` copied. Best-effort: these are copies, and the item's own
    photographs are untouched in the media store either way."""
    shutil.rmtree(paths.publish_staging_dir() / str(item_id), ignore_errors=True)


def can_drive(market: str) -> bool:
    """Whether this marketplace can be published to by driving its form."""
    adapter = market_adapters.get_adapter(market)
    return bool(adapter and adapter.publish_fields_js)
