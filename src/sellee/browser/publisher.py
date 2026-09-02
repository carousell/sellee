"""Publishing a listing by driving the form, rather than by asking a model to.

A recipe pass works and is what Carousell uses, but it costs about $1.54 a listing in model turns
to do something deterministic. The two coexist: `supported_markets` asks whether a market has a
recipe **or** a publish surface.

The shape is `browser/sink.py`'s, because a publish commits something the seller cannot take back
and the moment of commit is the one place we cannot see. Hence two exception types:

  * before the commit, `PublishNotAttempted`: nothing was created, the caller may try again;
  * from the commit onward, `PublishUnverified`: a listing may exist, and re-driving would give
    the seller two.

What is market-specific — the artifacts and the individual steps — lives on the adapter's
`markets.publishing.PublishSurface`. What is ours — the order of the steps, the exception bracket
above, the human pacing — lives here, in a module function no market module can override.

Nothing here decides *what* to say — title, price, description and photographs arrive as an item.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from urllib.parse import urljoin

from sellee import paths
from sellee.browser import markets as market_adapters
from sellee.browser import reconcile
from sellee.browser.client import BrowserError
from sellee.browser.markets.publishing import (
    STEP_SETTLE_SEC,
    PublishNotAttempted,
    PublishOutcome,
    PublishUnverified,
)

__all__ = [
    "PublishNotAttempted",
    "PublishOutcome",
    "PublishUnverified",
    "can_drive",
    "clear_staged",
    "publish",
    "stage_photos",
]

log = logging.getLogger(__name__)


def publish(
    client, adapter, item: dict, *, create_url: str, photos=(), listings_url=None, sleep=None
) -> PublishOutcome:
    """Fill this market's create form from an item and publish it.

    Answers a `PublishOutcome`. Raises `PublishNotAttempted` when nothing was created and
    `PublishUnverified` when something may have been — never a bare `BrowserError`, because the
    caller's decision to retry turns entirely on which of those two it is.
    """
    surface = adapter.publish
    if surface is None:
        raise PublishNotAttempted(f"{adapter.market} has no publish surface")
    pause = sleep or _sleep

    client.navigate_visible(create_url)
    pause(STEP_SETTLE_SEC)
    surface.open_form(client, pause)
    found = client.evaluate(surface.fields_js) or {}
    _refuse_unless_ready(surface, found)

    if photos:
        surface.attach_photos(client, photos, found, pause)
        pause(STEP_SETTLE_SEC)
    surface.fill(client, item, found)
    condition = surface.map_condition(str(item.get("condition") or ""))
    surface.choose_option(client, "condition", condition, found, pause)
    surface.choose_option(client, "category", surface.default_category, found, pause)
    surface.refuse_paid_extras(client)
    surface.verify_form(client, item)

    # Everything past here may have created a listing.
    return _commit(client, adapter, surface, item, listings_url, pause)


def _refuse_unless_ready(surface, found: dict) -> None:
    missing = [step for step in surface.required_fields if step not in (found.get("marked") or [])]
    if missing:
        raise PublishNotAttempted(
            f"the {surface.market} create form is missing {missing} — nothing was filled in",
            retryable=True,
        )


def _commit(client, adapter, surface, item: dict, listings_url, pause) -> PublishOutcome:
    """Press through the form's own steps, and find out what was made.

    Everything in here is past the point of no return, so every failure is `PublishUnverified`:
    re-driving any of them would give the seller two listings.
    """
    # Last check on the safe side of the line. A marketplace greys Next out until it has
    # everything it requires (Facebook: no photo, no Next), and clicking a disabled button
    # submits nothing — treating that as "may have gone through" would retire the item over a
    # missing photograph. Read fresh: the caller's `found` predates filling, when Next is always
    # disabled.
    ready = client.evaluate(surface.fields_js) or {}
    if ready.get("next_enabled") is False:
        raise PublishNotAttempted(
            f"the {surface.market} form will not accept this listing yet — it still wants "
            "something, and nothing was submitted"
        )
    try:
        surface.commit(client, pause)

        # Reading the result stays inside the bracket on purpose: it runs on a page that just
        # navigated, the likeliest moment for a browser call to fail, and a failure here means a
        # listing that probably exists and cannot be named. Outside the bracket it escaped as a
        # bare `BrowserError`, no ledger row was written, and the next tick made a second copy.
        result = client.evaluate(surface.result_js) or {}
        listing_id = result.get("listing_id")
        if listing_id:
            return PublishOutcome(
                listing_id=str(listing_id), url=str(result.get("url") or ""), verified=True
            )

        # The page we land on may not name the listing (Facebook redirects to its selling page,
        # whose cards carry no id), so ask the seller's own listings instead. That read is the
        # listings surface's, not the publish surface's, which is why the adapter is still in
        # hand here.
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
    return bool(adapter is not None and adapter.publish is not None)
