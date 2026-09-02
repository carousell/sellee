"""What a market must provide to be published to by driving its form, and the two ways that fails.

Each market owns its whole pre-commit choreography as one `prepare`, and its commit as one
`commit`. There is no shared sequence to override, because create forms do not share one: one
market detects a category from the photographs and has a mandatory delivery step, another picks a
category by name and presses Next before Publish.

What is *ours* is the flow in `browser/publisher.py`: the exception bracket that separates "nothing
was created" from "a listing may exist", the two gates below that run between `prepare` and
`commit` whatever the choreography did, reading the outcome, and human pacing. The flow holds the
surface and is never held by it, so a market has no method it could override to publish without
them. This module imports nothing above the browser client, so a market module can subclass it
without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from sellee.browser.client import BrowserError

# How long a form is given to settle between steps, in seconds. A dropdown fetches its options.
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


# The pieces a surface cannot be built without, and why each one is not optional:
#
#   * `market` names the market in every refusal the seller eventually reads;
#   * `readback_js` is the gate that stops an unchecked listing going live;
#   * `prepare` and `commit` are the choreography — there is no default flow to inherit;
#   * `verify_form` and `refuse_paid_extras` are the two gates. A market with nothing to refuse
#     still writes the method, so that weakening is visible in the market's own file.
#
# Checked when the subclass is created rather than when a publish runs: a half-configured surface
# should fail at import, not silently skip a gate on a seller's listing.
_REQUIRED_ARTIFACTS = ("market", "readback_js")
_REQUIRED_METHODS = ("prepare", "commit", "verify_form", "refuse_paid_extras")


class PublishSurface:
    """One market's create form: its artifacts, its choreography, and its gates.

    Presence is the capability — an adapter carrying one of these can be driven-published, and the
    checks upstream ask exactly that.

    The artifacts:

      * `readback_js` answers what the form holds now, `{title, price, ...}`, for the gate that
        runs before anything is pressed. Required.
      * `result_js` names the listing that was made, `{listing_id, url}`. Optional: a market whose
        landing page names nothing leaves it empty and is confirmed from the seller's own listings
        instead.
      * `fields_js` is optional too, for a form whose controls no CSS selector can reach: the
        artifact finds and marks them and says which it found. A market that needs no marking pass
        leaves it empty; only that market's own `prepare` reads it.
    """

    # Named in messages and logs; the same string as the owning adapter's `market`.
    market = ""

    fields_js = ""
    readback_js = ""
    result_js = ""

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        missing = [name for name in _REQUIRED_ARTIFACTS if not getattr(cls, name, "")]
        missing += [
            name
            for name in _REQUIRED_METHODS
            if getattr(cls, name, None) is getattr(PublishSurface, name)
        ]
        if missing:
            raise TypeError(f"{cls.__name__} is not a usable publish surface: it has no {missing}")

    def prepare(self, client, item: dict, photos, pause) -> None:
        """Fill this market's create form, stopping short of anything irreversible.

        Called before the flow's commit bracket, so a failure here means nothing was created:
        raise `PublishNotAttempted`. Raise `PublishUnverified` only for a form that crossed its own
        point of no return early — the flow passes that through untouched, and re-driving it would
        give the seller two listings.

        `photos` are paths the browser server may read; `pause(seconds)` is the human pacing. What
        to say — title, price, description, photographs — arrives in `item`; choosing it is the
        listing flow's judgement, never a driver's.
        """
        raise NotImplementedError(f"{type(self).__name__} does not say how to fill its form")

    def commit(self, client, pause) -> None:
        """Press through whatever this form calls publishing.

        Called inside the flow's bracket: from the first press onward a listing may exist, so a
        form that misbehaves here raises `PublishUnverified`, never `PublishNotAttempted`.
        """
        raise NotImplementedError(f"{type(self).__name__} does not say how to publish")

    def verify_form(self, client, item: dict) -> None:
        """Read the form back and refuse unless it agrees with the item.

        A gate, not a step: the flow calls it after `prepare` whatever `prepare` did. This is the
        last moment a mistake is free — a field that truncated or refused its input otherwise
        becomes a live listing the seller has to find and fix. `forms.check_readback` is the usual
        implementation.
        """
        raise NotImplementedError(f"{type(self).__name__} does not say how to read its form back")

    def refuse_paid_extras(self, client) -> None:
        """Refuse to publish while anything on this form would spend the seller's money.

        The other gate. Read rather than assumed: "it defaults to off" is the kind of belief that
        stops being true in a release nobody told us about.
        """
        raise NotImplementedError(f"{type(self).__name__} does not say what it refuses to spend")

    def map_condition(self, said: str) -> str:
        """This market's own word for an item's free-text condition; "" chooses nothing."""
        return ""
