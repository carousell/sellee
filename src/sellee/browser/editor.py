"""Changing a live listing by driving its edit form, rather than by asking a model to.

The sibling of `publisher.py`, and it shares that module's shape — the moment of commit is the one
place we cannot see, so the outcome is decided by which exception comes back:

  * before the commit, `ReviseNotAttempted`: nothing was saved;
  * from the commit onward, `ReviseUnverified`: the save may or may not have landed.

**The safety premise inverts here, which is why the taxonomy is not simply reused.** Re-driving a
create gives the seller two listings, so `PublishUnverified` is never retried. Re-driving an edit
sets the same price twice and the listing still shows it once: an edit is idempotent. So an
unverified edit is safe to try again within the lane's attempt bound, and the lane does.

Nothing here decides *what* the listing should say — that arrives as the item record, already
changed and already confirmed with the seller. And nothing here navigates to a guessed URL: the
form is reached from the item's recorded listing URL by pressing the page's own Edit control.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sellee.browser import formfill
from sellee.browser import markets as market_adapters
from sellee.browser.client import BrowserError
from sellee.browser.formfill import COMMIT_SETTLE_SEC, STEP_SETTLE_SEC

log = logging.getLogger(__name__)

# Item field -> the edit form's step that holds it.
_STEPS = {"title": "title", "list_price": "price", "description": "description"}


class ReviseNotAttempted(BrowserError):
    """Nothing was saved. `retryable` says whether another go could produce a different answer —
    a wall or a slow page may clear; a form that lacks the field never will."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ReviseUnverified(BrowserError):
    """The save was pressed and what it did could not be read back. Safe to repeat: an edit
    applied twice is the same edit."""


@dataclass(frozen=True)
class ReviseOutcome:
    """What the listing page showed afterwards, and whether that is what was asked for."""

    verified: bool
    accepted: dict = field(default_factory=dict)
    mismatched: tuple = ()
    reason: str = ""


def can_edit(market: str) -> bool:
    """Whether this marketplace can be edited by driving its form at all."""
    adapter = market_adapters.get_adapter(market)
    return bool(adapter and adapter.edit_fields_js)


def can_edit_fields(market: str, changed) -> bool:
    """Whether the driver can change every one of these fields on this marketplace. A change it
    can only half-make is not attempted — a listing showing a new price and an old photo set is
    worse than one the seller is told to fix by hand."""
    adapter = market_adapters.get_adapter(market)
    return bool(adapter and adapter.edit_fields_js and set(changed) <= _drivable(adapter))


def _drivable(adapter) -> set:
    """What this driver can actually change on this market: fields the adapter says its form holds
    AND fields this module knows how to type. An adapter naming a field with no step here would
    otherwise reach `revise` and fail on a lookup instead of being told it is not attempted."""
    return set(adapter.editable_fields) & set(_STEPS)


def _retryable(message: str) -> ReviseNotAttempted:
    return ReviseNotAttempted(message, retryable=True)


def revise(client, adapter, item: dict, *, listing_url: str, changed, sleep=None) -> ReviseOutcome:
    """Open this listing's edit form, change the named fields to what the item says, and save.

    Answers a `ReviseOutcome`, or raises `ReviseNotAttempted` / `ReviseUnverified` — never a bare
    `BrowserError`, because the caller's decision turns entirely on which side of the save it was.
    """
    if not adapter.edit_fields_js:
        raise ReviseNotAttempted(f"{adapter.market} has no edit selectors")
    outside = sorted(set(changed) - _drivable(adapter))
    if outside:
        raise ReviseNotAttempted(f"{', '.join(outside)} cannot be changed on {adapter.market} here")
    pause = sleep or formfill.sleep

    try:
        client.navigate_visible(listing_url)
    except BrowserError as exc:
        raise _retryable(f"the listing page would not open: {exc}") from exc
    pause(STEP_SETTLE_SEC)
    formfill.refuse_at_a_wall(client, adapter, _retryable)
    _open_form(client, adapter, pause)
    found = _mark_fields(client, adapter, pause)

    steps = [_STEPS[name] for name in sorted(changed)]
    missing = [step for step in steps + ["save"] if step not in (found.get("marked") or [])]
    if missing:
        raise _retryable(
            f"the {adapter.market} edit form is missing {missing} — nothing was changed"
        )

    formfill.type_fields(
        client,
        adapter.edit_target,
        _text_fields(item, changed),
        found.get("marked"),
        _retryable,
        pause,
        replace=True,
    )
    _refuse_paid_promotion(client, adapter)
    _verify_form(client, adapter, item, changed)

    ready = _evaluate(client, adapter.edit_fields_js)
    if ready.get("save_enabled") is False:
        raise ReviseNotAttempted(
            f"the {adapter.market} form will not save this change — it still wants something"
        )
    formfill.refuse_at_a_wall(client, adapter, _retryable)

    # Everything past here may have saved.
    try:
        client.click(adapter.edit_target("save"), "Save")
        pause(COMMIT_SETTLE_SEC)
        return _confirm(client, adapter, item, listing_url, changed, pause)
    except ReviseUnverified:
        raise
    except BrowserError as exc:
        raise ReviseUnverified(f"the change may have been saved: {exc}") from exc


def _evaluate(client, js: str) -> dict:
    try:
        return client.evaluate(js) or {}
    except BrowserError as exc:
        raise _retryable(f"the page could not be read: {exc}") from exc


def _open_form(client, adapter, pause) -> None:
    """Press the listing's own Edit control. A page with none is a listing this account does not
    own, or one that is no longer up — either way there is nothing here to change."""
    entry = _evaluate(client, adapter.edit_entry_js) if adapter.edit_entry_js else {}
    if not entry.get("found"):
        raise _retryable(f"the {adapter.market} listing page offers no Edit control")
    try:
        client.click(adapter.edit_target("entry"), "Edit listing")
    except BrowserError as exc:
        raise _retryable(f"the Edit control would not open: {exc}") from exc
    pause(STEP_SETTLE_SEC)


def _mark_fields(client, adapter, pause) -> dict:
    """Mark the form's controls, expanding whatever it keeps collapsed first (Facebook hides the
    description behind "More details"). The expand is best-effort: the marking decides."""
    found = _evaluate(client, adapter.edit_fields_js)
    if "more" in (found.get("marked") or []) and "description" not in found.get("marked"):
        try:
            client.click(adapter.edit_target("more"), "the rest of the fields")
            pause(STEP_SETTLE_SEC)
            found = _evaluate(client, adapter.edit_fields_js)
        except BrowserError:
            log.debug("could not expand the %s edit form", adapter.market, exc_info=True)
    return found


def _text_fields(item: dict, changed) -> list:
    wanted = set(changed)
    fields = []
    if "title" in wanted:
        fields.append(("title", item.get("title") or ""))
    if "list_price" in wanted:
        fields.append(("price", formfill.bare_price(item.get("list_price"))))
    if "description" in wanted:
        fields.append(("description", item.get("description") or ""))
    return fields


def _refuse_paid_promotion(client, adapter) -> None:
    """Never save with a paid boost switched on — it spends the seller's money unasked."""
    if not _evaluate(client, adapter.edit_fields_js).get("boost_on"):
        return
    try:
        client.click(adapter.edit_target("boost"), "the paid boost")
    except BrowserError as exc:
        raise _retryable(f"the paid boost was on and would not turn off: {exc}") from exc
    if _evaluate(client, adapter.edit_fields_js).get("boost_on"):
        raise ReviseNotAttempted("the paid boost is still on — refusing to save")


def _verify_form(client, adapter, item: dict, changed) -> None:
    """Read the form back before saving — the last moment a mistake is free. The likeliest one on
    an edit is a box that did not empty, which would save "150120" as the price."""
    if not adapter.edit_readback_js:
        return
    seen = _evaluate(client, adapter.edit_readback_js)
    wanted = set(changed)
    if "title" in wanted and _norm(seen.get("title")) != _norm(item.get("title")):
        raise ReviseNotAttempted(f"the form shows the title as {seen.get('title')!r}")
    if "list_price" in wanted:
        price = item.get("list_price")
        if formfill.read_price(seen.get("price")) is None or not formfill.price_matches(
            seen.get("price"), price
        ):
            raise ReviseNotAttempted(f"the form shows the price as {seen.get('price')!r}")
    if "description" in wanted and _norm(seen.get("description")) != _norm(item.get("description")):
        raise ReviseNotAttempted("the form's description is not what was asked for")


def _confirm(client, adapter, item: dict, listing_url: str, changed, pause) -> ReviseOutcome:
    """Reopen the listing's edit form and say which of the changed fields it now holds.

    The form, not the listing page: its inputs are exactly what the marketplace stored, found by
    the labels the edit already relied on. The listing page is a layout to be scraped, and on a
    live Facebook listing its title read came back as a "Renew your listing?" banner — a check
    built on it would call a working title edit a failure.

    Per field, so a partial save names exactly what is still wrong. A form that will not reopen
    says nothing either way, which is unverified rather than failed — the lane may look again.
    Nothing is typed here, so leaving the reopened form discards nothing.
    """
    client.navigate_visible(listing_url)
    pause(STEP_SETTLE_SEC)
    entry = client.evaluate(adapter.edit_entry_js) or {}
    if not entry.get("found"):
        raise ReviseUnverified("saved, but the edit form would not reopen to check it")
    client.click(adapter.edit_target("entry"), "Edit listing")
    pause(STEP_SETTLE_SEC)
    client.evaluate(adapter.edit_fields_js)  # marks the controls the read-back reads
    seen = client.evaluate(adapter.edit_readback_js) or {}
    if not seen.get("title"):
        raise ReviseUnverified("saved, but the edit form could not be read back")

    accepted = {name: seen.get(name) for name in ("title", "price", "description")}
    mismatched = []
    wanted = set(changed)
    if "title" in wanted and _norm(seen.get("title")) != _norm(item.get("title")):
        mismatched.append("title")
    # A price box that did not load reads as nothing, and nothing is not the new price:
    # `price_matches` treats silence as "no evidence", which is right for a caller that may carry
    # on, and wrong here, where the answer is whether the listing shows what was asked.
    if "list_price" in wanted and (
        formfill.read_price(seen.get("price")) is None
        or not formfill.price_matches(seen.get("price"), item.get("list_price"))
    ):
        mismatched.append("list_price")
    if "description" in wanted and _norm(seen.get("description")) != _norm(item.get("description")):
        mismatched.append("description")
    return ReviseOutcome(
        verified=not mismatched,
        accepted=accepted,
        mismatched=tuple(mismatched),
        reason=f"it still shows the old {', '.join(mismatched)}" if mismatched else "",
    )


def _norm(text) -> str:
    return " ".join(str(text or "").split())
