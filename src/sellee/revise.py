"""The revise lane: carry a seller's edit out to every browser marketplace, and say how it went.

`update_live_listing` changes the record and carousell.ai in the turn the seller asks, and leaves a
`listing_revisions` row for each browser marketplace the item is live on. This lane drains those
rows. It is modelled on the fan-out lane (`crosslist.py`) and keeps its rules for the same reasons:

  * Work is derived from durable rows, so a restart resumes it and nothing depends on a model
    remembering what it started.
  * One at a time, and never while something else holds the shared tab — a driven edit holds the
    browser for its whole duration, and the inbox read and the reply sink wait behind it.
  * Bounded, spaced attempts: each one is minutes of driving someone's logged-in account.
  * The seller is told by the daemon, from the row. A background edit has no conversation to
    report into, and "I changed it" must never be said by something that did not see it change.

Conditions that fix themselves (Chrome closed, the tab busy) are checked *before* a row is claimed,
so they cost it nothing. Conditions that will not (the item sold, the marketplace disconnected or
asking us to stop, no way to edit it) are checked after, and settle the row as failed with a
message the seller can act on.

Like the driven publish, a driven edit takes no pacing reserve and is not held by quiet hours: an
edited listing sits there until someone looks at it, so the hour it changed is not what anyone
sees, and the one-at-a-time and attempt bounds are what keep it from being a burst.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from sellee import marketplaces, settings
from sellee.browser import editor
from sellee.browser import markets as market_adapters
from sellee.browser.client import BrowserError, BrowserUnavailable
from sellee.browser.inbox import browser_busy

log = logging.getLogger(__name__)

REVISE_MAX_ATTEMPTS = 3
REVISE_RETRY_AFTER_SEC = 15 * 60.0
# A driven edit runs inside one lane tick; a row still `running` with no pass this long after its
# claim was left behind by a crash, and would otherwise hold the lane shut forever.
STALE_RUNNING_SEC = 30 * 60.0

_FIELD_WORDS = {
    "title": "title",
    "description": "description",
    "list_price": "price",
    "photos": "photos",
}

REVISED_NOTICE = "{item} on {market} now shows the new {fields}: {url}"
FAILED_NOTICE = (
    "I couldn't change the {fields} on {item}'s {market} listing, so it still shows the old "
    "one ({reason}). You can change it there yourself: {url}"
)


@dataclass
class ReviseDeps:
    store: object
    bus: object
    config: object
    # The daemon's browser acquisition: calling it verifies Node and makes Chrome answer, or raises
    # BrowserUnavailable.
    browser_factory: Callable[[], object]
    now: Callable[[], float] = time.time


def revise_lane(deps: ReviseDeps) -> None:
    """One tick: report the edits that have settled, then drive at most one more."""
    report_settled(deps)
    if deps.store.is_paused():
        return
    settle_orphans(deps)
    run_next(deps)


# --- driving the next edit ---------------------------------------------------------------------


def run_next(deps: ReviseDeps) -> str | None:
    """Claim and carry out the next due edit, if nothing is in the way. Returns its id."""
    if deps.store.revision_in_flight() or browser_busy(deps.store):
        return None
    upcoming = deps.store.next_listing_revision(retry_after_sec=REVISE_RETRY_AFTER_SEC)
    if upcoming is None:
        return None
    try:
        deps.browser_factory()
    except BrowserUnavailable as exc:
        deps.bus.publish("browser.unavailable", {"reason": str(exc)})
        return None

    revision = deps.store.claim_listing_revision(retry_after_sec=REVISE_RETRY_AFTER_SEC)
    if revision is None:
        return None
    item = deps.store.get_item(revision["item_id"])
    refusal = _refusal(deps, revision, item)
    if refusal:
        _settle(deps, revision, status="failed", error=refusal)
        return revision["revision_id"]

    market = revision["market"]
    if editor.can_edit_fields(market, revision["changed"]):
        _drive(deps, revision, item)
    else:
        _enqueue_pass(deps, revision)
    return revision["revision_id"]


def _refusal(deps: ReviseDeps, revision: dict, item) -> str:
    """Why this edit can never happen now, or "" when it can. Asked at claim time, not queue time:
    the claim may run long after the ask, and the world can have moved in between."""
    market = revision["market"]
    if item is None:
        return "the item is gone"
    if item["id"] in deps.store.sold_item_ids():
        return "the item has sold"
    if not (item.get("listing_urls") or {}).get(market):
        return "it is no longer listed there"
    if market not in settings.connected_markets(deps.store):
        return f"{marketplaces.display_name(market)} is disconnected"
    if deps.store.market_block(market):
        return f"{marketplaces.display_name(market)} has asked us to stop for now"
    if not (editor.can_edit_fields(market, revision["changed"]) or marketplaces.edit_flow(market)):
        return "that change can't be made there automatically"
    return ""


def _drive(deps: ReviseDeps, revision: dict, item: dict) -> None:
    """Change the listing by driving its edit form, and record what happened."""
    market = revision["market"]
    adapter = market_adapters.get_adapter(market)
    url = item["listing_urls"][market]
    try:
        client = deps.browser_factory()
        with client.exclusive():
            outcome = editor.revise(
                client, adapter, item, listing_url=url, changed=revision["changed"]
            )
    except editor.ReviseNotAttempted as exc:
        _settle_or_retry(deps, revision, str(exc), retryable=exc.retryable)
        return
    except editor.ReviseUnverified as exc:
        # Idempotent, unlike a publish: pressing Save again sets the same values again.
        _settle_or_retry(deps, revision, str(exc), retryable=True)
        return
    except BrowserUnavailable as exc:
        # Checked before the claim, so reaching here means Chrome went away mid-edit. That spends
        # the attempt like any other interruption: handing the row back uncounted would retry it
        # every interval, forever, without the seller ever hearing the edit had not happened.
        deps.bus.publish("browser.unavailable", {"reason": str(exc)})
        _settle_or_retry(deps, revision, f"the browser went away: {exc}", retryable=True)
        return
    except BrowserError as exc:
        _settle_or_retry(deps, revision, f"unexpected: {exc}", retryable=True)
        return

    deps.bus.publish(
        "revise.driven",
        {
            "item_id": item["id"],
            "market": market,
            "verified": outcome.verified,
            "mismatched": list(outcome.mismatched),
        },
    )
    if outcome.verified:
        _settle(deps, revision, status="done", accepted=outcome.accepted)
    else:
        _settle_or_retry(deps, revision, outcome.reason, retryable=True, accepted=outcome.accepted)


def _enqueue_pass(deps: ReviseDeps, revision: dict) -> None:
    """Hand a recipe market's edit to a model pass. The row stays `running` until the pass records
    the outcome, or `settle_orphans` notices the pass ended without doing so."""
    pass_id = deps.store.enqueue_pass(
        "edit",
        {
            "revision_id": revision["revision_id"],
            "item_id": revision["item_id"],
            "market": revision["market"],
            "changed": revision["changed"],
        },
    )
    deps.store.attach_revision_pass(revision["revision_id"], pass_id)
    deps.bus.publish(
        "revise.queued_pass",
        {"item_id": revision["item_id"], "market": revision["market"]},
        pass_id=pass_id,
    )


def _settle_or_retry(
    deps: ReviseDeps, revision: dict, error: str, *, retryable: bool, accepted=None
) -> None:
    if retryable and revision["attempts"] < REVISE_MAX_ATTEMPTS:
        deps.store.finish_listing_revision(
            revision["revision_id"], status="pending", retry=True, error=error
        )
        return
    _settle(deps, revision, status="failed", error=error, accepted=accepted)


def _settle(deps: ReviseDeps, revision: dict, *, status: str, error=None, accepted=None) -> None:
    deps.store.finish_listing_revision(
        revision["revision_id"], status=status, error=error, accepted=accepted
    )
    deps.bus.publish(
        "revise.settled",
        {
            "item_id": revision["item_id"],
            "market": revision["market"],
            "status": status,
            "error": (error or "")[:200],
        },
    )


def settle_orphans(deps: ReviseDeps) -> int:
    """Settle `running` rows whose work is over but was never recorded. Returns how many.

    Two ways a row is orphaned: its edit pass ended without calling `record_listing_revision`, or
    the daemon died mid-drive. Either way the row would hold the one-at-a-time gate shut forever,
    so it is handed back for another go — or failed, once its attempts are spent.
    """
    settled = 0
    for revision in deps.store.list_listing_revisions("running"):
        pass_id = revision.get("pass_id")
        if pass_id:
            row = deps.store.get_pass(pass_id) or {}
            if row.get("status") in ("queued", "running"):
                continue
            reason = "the edit pass ended without saying how it went"
        else:
            claimed = revision.get("claimed_ts") or 0
            if deps.now() - claimed < STALE_RUNNING_SEC:
                continue
            reason = "the edit was interrupted"
        _settle_or_retry(deps, revision, reason, retryable=True)
        settled += 1
    return settled


# --- reporting ---------------------------------------------------------------------------------


def fields_phrase(changed) -> str:
    """The changed fields as words a seller reads: "price", "price and description"."""
    words = [_FIELD_WORDS.get(name, name) for name in changed]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def report_settled(deps: ReviseDeps) -> int:
    """Tell the seller how each settled edit went. Returns how many were reported."""
    reported = 0
    owed: dict = {}
    for status in ("pending", "running"):
        for row in deps.store.list_listing_revisions(status):
            owed.setdefault((row["item_id"], row["market"]), []).append(set(row["changed"]))
    for revision in deps.store.unreported_listing_revisions():
        item = deps.store.get_item(revision["item_id"]) or {}
        market = revision["market"]
        newer = owed.get((revision["item_id"], market), [])
        if any(set(revision["changed"]) <= fields for fields in newer):
            # A newer edit for this listing is on its way and covers every field this one touched,
            # so this result is about values that are no longer the ask: it closes silently and
            # the newer row reports for both. Only when it covers them all — a newer price edit
            # must not swallow the news that a description edit failed, or that one landed.
            deps.store.report_listing_revision(revision["revision_id"], None)
            continue
        values = {
            "item": item.get("title") or "the item",
            "market": marketplaces.display_name(market),
            "fields": fields_phrase(revision["changed"]),
            "url": (item.get("listing_urls") or {}).get(market) or "",
            "reason": revision.get("last_error") or "it did not take",
        }
        template = REVISED_NOTICE if revision["status"] == "done" else FAILED_NOTICE
        if deps.store.report_listing_revision(
            revision["revision_id"], template.format(**values), ref=revision["item_id"]
        ):
            reported += 1
            deps.bus.publish(
                "revise.reported",
                {
                    "item_id": revision["item_id"],
                    "market": market,
                    "ok": revision["status"] == "done",
                },
            )
    return reported
