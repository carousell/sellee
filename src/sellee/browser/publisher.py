"""Publishing a listing by driving the form, rather than by asking a model to.

A recipe pass works and is what Carousell uses, but it costs about $1.54 a listing in model turns
to do something deterministic. The two coexist: `supported_markets` asks whether a market has a
recipe **or** publish selectors.

The shape is `browser/sink.py`'s, because a publish commits something the seller cannot take back
and the moment of commit is the one place we cannot see. Hence two exception types:

  * before the commit, `PublishNotAttempted`: nothing was created, the caller may try again;
  * from the commit onward, `PublishUnverified`: a listing may exist, and re-driving would give
    the seller two.

Nothing here decides *what* to say — title, price, description and photographs arrive as an item.
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

# The fields that must be marked before anything is typed. The two text inputs are
# indistinguishable except by label, so a partly-recognised form could put the price in the title.
REQUIRED_FIELDS = ("title", "price", "next")

# How long the form is given to settle between steps, in seconds. A dropdown fetches its options.
STEP_SETTLE_SEC = 2.0
COMMIT_SETTLE_SEC = 6.0


class PublishNotAttempted(BrowserError):
    """Nothing was created.

    `retryable` says whether trying again could produce a different answer; the caller turns that
    into "leave the pair eligible" or "spend its shot". Defaults to False because most refusals
    are about the shape of the form or the item, and neither changes on a retry.
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


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
    condition = adapter.publish_condition_for(str(item.get("condition") or ""))
    _choose(client, adapter, "condition", condition, found, pause)
    _choose(client, adapter, "category", adapter.publish_default_category, found, pause)
    _refuse_paid_promotion(client, adapter)
    _verify_form(client, adapter, item)

    # Everything past here may have created a listing.
    return _commit(client, adapter, item, listings_url, pause)


def _open_all_fields(client, adapter, pause) -> None:
    """Expand whatever the form keeps collapsed (Facebook hides the description behind "More
    details"). Best-effort: the marking pass that follows decides whether the form is usable."""
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
            f"the {market} create form is missing {missing} — nothing was filled in",
            retryable=True,
        )


def _attach(client, adapter, photos, found: dict, pause) -> None:
    """Hand the item's photographs to the form.

    The control that opens a file chooser is pressed first, because the browser server only
    accepts an upload while a chooser is open. Facebook requires a photo and leaves Next greyed
    out until it has one.
    """
    if "add_photos" in (found.get("marked") or []):
        try:
            client.call_tool(
                "browser_click",
                {"target": adapter.publish_target("add_photos"), "element": "Add photos"},
            )
            pause(STEP_SETTLE_SEC)
        except BrowserError as exc:
            raise PublishNotAttempted(
                f"the photo chooser would not open: {exc}", retryable=True
            ) from exc
    try:
        client.call_tool("browser_file_upload", {"paths": [str(path) for path in photos]})
    except BrowserError as exc:
        raise PublishNotAttempted(
            f"the photographs would not attach: {exc}", retryable=True
        ) from exc


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
            raise PublishNotAttempted(f"could not fill {step}: {exc}", retryable=True) from exc


def _text_fields(item: dict) -> list:
    price = item.get("list_price")
    return [
        ("title", item.get("title") or ""),
        # Typed bare: the field formats what it is given, and a grouped "1,299" has been read
        # as 1 by more than one marketplace form.
        (
            "price",
            f"{price:.0f}" if isinstance(price, (int, float)) and price == int(price) else price,
        ),
        ("description", item.get("description") or ""),
    ]


def _choose(client, adapter, step: str, wanted: str, found: dict, pause) -> None:
    """Open one dropdown and pick an option by name.

    An unsatisfiable dropdown is fatal before the commit: Facebook requires both, so carrying on
    would press Publish against a form that refuses.
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
        raise PublishNotAttempted(f"could not choose a {step}: {exc}", retryable=True) from exc


def _refuse_paid_promotion(client, adapter) -> None:
    """Never publish with a paid boost switched on — it spends the seller's money unasked. Read
    rather than assumed: "it defaults to off" is the kind of belief that stops being true in a
    release nobody told us about."""
    found = client.evaluate(adapter.publish_fields_js) or {}
    if not found.get("boost_on"):
        return
    try:
        client.call_tool(
            "browser_click",
            {"target": adapter.publish_target("boost"), "element": "the paid boost switch"},
        )
    except BrowserError as exc:
        raise PublishNotAttempted(
            f"the paid boost was on and would not turn off: {exc}", retryable=True
        ) from exc
    if (client.evaluate(adapter.publish_fields_js) or {}).get("boost_on"):
        raise PublishNotAttempted("the paid boost is still on — refusing to publish")


def _verify_form(client, adapter, item: dict) -> None:
    """Read the form back before pressing anything — the last moment a mistake is free. A field
    that truncated or refused its input otherwise becomes a live listing the seller has to fix."""
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

    Everything in here is past the point of no return, so every failure is `PublishUnverified`:
    re-driving any of them would give the seller two listings.
    """
    # Last check on the safe side of the line. A marketplace greys Next out until it has
    # everything it requires (Facebook: no photo, no Next), and clicking a disabled button
    # submits nothing — treating that as "may have gone through" would retire the item over a
    # missing photograph. Read fresh: the caller's `found` predates filling, when Next is always
    # disabled.
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

        # Reading the result stays inside the bracket on purpose: it runs on a page that just
        # navigated, the likeliest moment for a browser call to fail, and a failure here means a
        # listing that probably exists and cannot be named. Outside the bracket it escaped as a
        # bare `BrowserError`, no ledger row was written, and the next tick made a second copy.
        result = client.evaluate(adapter.publish_result_js) or {}
        listing_id = result.get("listing_id")
        if listing_id:
            return PublishOutcome(
                listing_id=str(listing_id), url=str(result.get("url") or ""), verified=True
            )

        # The page we land on may not name the listing (Facebook redirects to its selling page,
        # whose cards carry no id), so ask the seller's own listings instead.
        found = _confirm_by_title(client, adapter, item, listings_url, pause)
        if found is not None:
            return found
        # Unverified, not an error: left for a human rather than retried into a duplicate.
        return PublishOutcome(
            listing_id=None,
            url=str(result.get("url") or ""),
            verified=False,
            reason="published, but the new listing could not be identified afterwards",
        )
    except PublishUnverified:
        raise
    except BrowserError as exc:
        raise PublishUnverified(f"the publish may have gone through: {exc}") from exc


def _confirm_by_title(client, adapter, item: dict, listings_url, pause) -> PublishOutcome | None:
    """Find the listing we just made among the seller's own, by title.

    Only used to confirm, never to decide whether to publish — the listing exists either way, and
    the alternative to a title match is a human going to look. Ambiguity abstains: with two live
    listings of the same title, claiming the wrong id would record a URL pointing at the older
    one, and buyers on the new listing would never join this item.
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


# How much a settle may vary either side. The same fields in the same order at fixed millisecond
# pauses is nobody's way of filling a form; the point is variance, not slowness, so the average
# pause is unchanged.
_JITTER = 0.4


def _sleep(seconds: float) -> None:
    import random
    import time

    time.sleep(random.uniform(seconds * (1 - _JITTER), seconds * (1 + _JITTER)))


def stage_photos(item_id: str, photos) -> list:
    """Copy an item's photographs somewhere the browser server will read them.

    The server only opens files under its own roots, so the media store is out of reach — fatal on
    a marketplace that requires a picture. Copied, not moved: the media store is the item's own
    record. Answers the staged paths in order, skipping failures — three photographs of four is
    worth publishing.
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
        candidate = Path(_photo_path(source))
        if not candidate.name:
            continue
        destination = target / f"{index:02d}{candidate.suffix or '.jpg'}"
        try:
            shutil.copyfile(candidate, destination)
        except OSError:
            log.warning("could not stage %s for publishing", candidate, exc_info=True)
            continue
        staged.append(str(destination))
    return staged


def _photo_path(photo) -> str:
    """An item stores each photograph as `{"path": ..., "uploaded_url": ...}`; take the path, never
    `str(photo)` of the whole mapping. A bare string is still accepted."""
    if isinstance(photo, dict):
        return str(photo.get("path") or "")
    return str(photo or "")


def clear_staged(item_id: str) -> None:
    """Drop what `stage_photos` copied. Best-effort: these are copies, and the item's own
    photographs are untouched in the media store either way."""
    shutil.rmtree(paths.publish_staging_dir() / str(item_id), ignore_errors=True)


def can_drive(market: str) -> bool:
    """Whether this marketplace can be published to by driving its form."""
    adapter = market_adapters.get_adapter(market)
    return bool(adapter and adapter.publish_fields_js)
