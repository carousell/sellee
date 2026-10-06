"""Craigslist posts: the category each goes in, and whether it is still up. A post is read as any
visitor reads it, outside the browser; a flagged one goes up again in the next category."""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from typing import Callable

from sellee import marketplaces
from sellee.browser.markets import craigslist

log = logging.getLogger(__name__)

MARKET = marketplaces.CRAIGSLIST
# How long after a post goes up each check is due; flags mostly come within minutes.
CHECK_AFTER_SEC = (60.0, 300.0, 1800.0, 3 * 3600.0)
FETCH_TIMEOUT_SEC = 15
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)
# Not the crosslist lane's origin, so a retired row owes the seller no publish report.
RETIRE_ORIGIN = "post_check"
REMOVED = (craigslist.POST_FLAGGED, craigslist.POST_GONE)

REPOST_NOTICE = (
    'Craigslist took down your "{title}" post after it was flagged, most likely for its category '
    "({was}). I'll post it again under {next}."
)
FLAGGED_NOTICE = (
    'Craigslist took down your "{title}" post after it was flagged. I\'ve tried the categories '
    "that fit, so I won't post it there again. Its carousell.ai listing is unaffected."
)
GONE_NOTICE = (
    'Your Craigslist post for "{title}" is no longer up, so I\'ve stopped using it. Its '
    "carousell.ai listing is unaffected."
)


def fetch_state(url: str) -> str:
    """One read of a post's public page, as `craigslist.post_state` names it."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SEC) as response:
            return craigslist.post_state(response.status, _body(response))
    except urllib.error.HTTPError as exc:
        return craigslist.post_state(exc.code, _body(exc))
    except (OSError, ValueError) as exc:
        log.info("could not read %s: %s", url, exc)
        return craigslist.POST_UNKNOWN


def _body(response) -> str:
    try:
        return response.read(200_000).decode("utf-8", "replace")
    except OSError:
        return ""


def _posts(index, item_id: str) -> list:
    return [row for row in index if row.get("item_id") == item_id and row.get("market") == MARKET]


def next_category(item: dict, index) -> str | None:
    """The best category this item has not been posted or tried in, or None when none is left."""
    tried = {row.get("category") for row in _posts(index, item["id"])}
    return next((c for c in craigslist.categories_for(item) if c not in tried), None)


def _last_post(index, item_id: str) -> dict | None:
    done = [row for row in _posts(index, item_id) if row.get("status") == "done"]
    return max(done, key=lambda row: row.get("finished_ts") or 0, default=None)


def check(store, bus, item: dict, fetch: Callable[[str], str] | None = None) -> str:
    """Read the item's post and act on a removal; returns the post's state. A removed post's URL is
    dropped and, unless the item sold, the seller told: flagged with a category left, it reposts."""
    url = (item.get("listing_urls") or {}).get(MARKET)
    if not url:
        return craigslist.POST_UNKNOWN
    state = (fetch or fetch_state)(url)
    bus.publish("craigslist.post_checked", {"item_id": item["id"], "state": state})
    if state not in REMOVED:
        return state
    store.archive_listing_url(item["id"], MARKET)
    if item["id"] in store.sold_item_ids():
        return state
    title = item.get("title") or "your item"
    index = store.publish_pass_index()
    upcoming = next_category(item, index) if state == craigslist.POST_FLAGGED else None
    if upcoming:
        was = (_last_post(index, item["id"]) or {}).get("category") or craigslist.DEFAULT_CATEGORY
        store.queue_notice(REPOST_NOTICE.format(title=title, was=was, next=upcoming))
    else:
        store.record_driven_publish(
            item["id"], MARKET, status="error", origin=RETIRE_ORIGIN, retired=True
        )
        notice = FLAGGED_NOTICE if state == craigslist.POST_FLAGGED else GONE_NOTICE
        store.queue_notice(notice.format(title=title))
    bus.publish(
        "craigslist.post_removed",
        {"item_id": item["id"], "state": state, "next_category": upcoming},
    )
    return state


def sweep(store, bus, checked: dict, now: float, fetch: Callable[[str], str] | None) -> None:
    """Check at most one post that has a check due. `checked` holds the last check made per
    post, in process: a restart costs one more read."""
    index = store.publish_pass_index()
    sold = store.sold_item_ids()
    for item in store.list_items():
        url = item["listing_urls"].get(MARKET)
        if not url or item["id"] in sold:
            continue
        posted = _last_post(index, item["id"])
        if posted is None:
            continue
        age = now - (posted.get("finished_ts") or now)
        due = sum(1 for after in CHECK_AFTER_SEC if age >= after)
        if due == 0 or checked.get(url, 0) >= due:
            continue
        checked[url] = due
        check(store, bus, item, fetch)
        return
