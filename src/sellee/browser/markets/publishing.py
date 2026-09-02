"""Driving a marketplace's create form: the steps a market may own, and the two ways it can fail.

The flow that orders these steps — and the commit bracket that decides which failure a caller
sees — lives in `browser/publisher.py`, deliberately not here. A market overrides a step below in
its own module; none of them can move the line between "nothing was created" and "a listing may
exist", because the steps never hold it. This module imports nothing above the browser client, so
a market module can subclass it without a cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sellee.browser.client import BrowserError

log = logging.getLogger(__name__)

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


class PublishSurface:
    """One market's create form: its artifacts, and the steps that drive them.

    Presence is the capability — an adapter carrying one of these can be driven-published, and
    the checks upstream ask exactly that. The artifacts' contracts:

      * `fields_js` marks every control and answers `{marked: [...], boost_on, next_enabled}`;
      * `readback_js` answers what the form holds, `{title, price}`, or "" to skip the check;
      * `result_js` names the listing that was made, `{listing_id, url}`;
      * `target(step)` is the selector for one marked control;
      * `options_js(wanted)` marks one dropdown option and answers `{chosen, options}`.

    The step defaults below are the first driven market's (Facebook's) flow promoted to defaults.
    A market whose form behaves differently overrides the step in its own module; the shared flow
    in `browser/publisher.py` does not change, so a tweak for one market cannot reach another's.
    """

    # Named for messages and logs; the same string as the owning adapter's `market`.
    market = ""

    # The fields that must be marked before anything is typed. The two text inputs are
    # indistinguishable except by label, so a partly-recognised form could put the price in the
    # title.
    required_fields = ("title", "price", "next")

    fields_js = ""
    readback_js = ""
    result_js = ""
    # Where a driver files a listing when nothing better is known — choosing a category from a
    # title is the listing flow's judgement, not a driver's.
    default_category = ""

    def target(self, step: str) -> str:
        raise NotImplementedError(f"{type(self).__name__} names no selector for its controls")

    def options_js(self, wanted: str) -> str:
        raise NotImplementedError(f"{type(self).__name__} has no dropdown-option artifact")

    def map_condition(self, said: str) -> str:
        """This market's own word for an item's free-text condition; "" chooses nothing."""
        return ""

    def open_form(self, client, pause) -> None:
        """Expand whatever the form keeps collapsed (Facebook hides the description behind "More
        details"). Best-effort: the marking pass that follows decides whether the form is
        usable."""
        found = client.evaluate(self.fields_js) or {}
        if "more" not in (found.get("marked") or []):
            return
        try:
            client.call_tool(
                "browser_click",
                {"target": self.target("more"), "element": "the rest of the listing fields"},
            )
            pause(STEP_SETTLE_SEC)
        except BrowserError:
            log.debug("could not expand the %s create form", self.market, exc_info=True)

    def attach_photos(self, client, photos, found: dict, pause) -> None:
        """Hand the item's photographs to the form.

        The control that opens a file chooser is pressed first, because the browser server only
        accepts an upload while a chooser is open. Facebook requires a photo and leaves Next
        greyed out until it has one.
        """
        if "add_photos" in (found.get("marked") or []):
            try:
                client.call_tool(
                    "browser_click",
                    {"target": self.target("add_photos"), "element": "Add photos"},
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

    def fill(self, client, item: dict, found: dict) -> None:
        """Type the fields that are text. Never `value =`: the form listens for real input, and a
        value set from script leaves React holding the old one — which publishes an empty
        listing."""
        for step, text in self.text_fields(item):
            if step not in (found.get("marked") or []) or not text:
                continue
            try:
                client.call_tool(
                    "browser_type",
                    {
                        "target": self.target(step),
                        "element": f"the {step} field",
                        "text": str(text),
                        "submit": False,
                    },
                )
            except BrowserError as exc:
                raise PublishNotAttempted(f"could not fill {step}: {exc}", retryable=True) from exc

    def text_fields(self, item: dict) -> list:
        price = item.get("list_price")
        return [
            ("title", item.get("title") or ""),
            # Typed bare: the field formats what it is given, and a grouped "1,299" has been read
            # as 1 by more than one marketplace form.
            (
                "price",
                f"{price:.0f}"
                if isinstance(price, (int, float)) and price == int(price)
                else price,
            ),
            ("description", item.get("description") or ""),
        ]

    def choose_option(self, client, step: str, wanted: str, found: dict, pause) -> None:
        """Open one dropdown and pick an option by name.

        An unsatisfiable dropdown is fatal before the commit: Facebook requires both, so carrying
        on would press Publish against a form that refuses.
        """
        if step not in (found.get("marked") or []) or not wanted:
            return
        try:
            client.call_tool(
                "browser_click",
                {"target": self.target(step), "element": f"the {step} dropdown"},
            )
            pause(STEP_SETTLE_SEC)
            answer = client.evaluate(self.options_js(wanted)) or {}
            if not answer.get("chosen"):
                raise PublishNotAttempted(
                    f"{self.market} offers no {step} called {wanted!r} "
                    f"(it offers {(answer.get('options') or [])[:8]})"
                )
            client.call_tool(
                "browser_click",
                {"target": self.target("option"), "element": f"the {step}"},
            )
            pause(STEP_SETTLE_SEC)
        except BrowserError as exc:
            if isinstance(exc, PublishNotAttempted):
                raise
            raise PublishNotAttempted(f"could not choose a {step}: {exc}", retryable=True) from exc

    def refuse_paid_extras(self, client) -> None:
        """Never publish with a paid boost switched on — it spends the seller's money unasked.
        Read rather than assumed: "it defaults to off" is the kind of belief that stops being true
        in a release nobody told us about."""
        found = client.evaluate(self.fields_js) or {}
        if not found.get("boost_on"):
            return
        try:
            client.call_tool(
                "browser_click",
                {"target": self.target("boost"), "element": "the paid boost switch"},
            )
        except BrowserError as exc:
            raise PublishNotAttempted(
                f"the paid boost was on and would not turn off: {exc}", retryable=True
            ) from exc
        if (client.evaluate(self.fields_js) or {}).get("boost_on"):
            raise PublishNotAttempted("the paid boost is still on — refusing to publish")

    def verify_form(self, client, item: dict) -> None:
        """Read the form back before pressing anything — the last moment a mistake is free. A
        field that truncated or refused its input otherwise becomes a live listing the seller has
        to fix."""
        if not self.readback_js:
            return
        seen = client.evaluate(self.readback_js) or {}
        title = (item.get("title") or "").strip()
        got = (seen.get("title") or "").strip()
        if title and got != title:
            raise PublishNotAttempted(f"the form shows the title as {got!r}, not {title!r}")
        price = item.get("list_price")
        digits = "".join(ch for ch in str(seen.get("price") or "") if ch.isdigit())
        if isinstance(price, (int, float)) and digits and int(digits) != int(price):
            raise PublishNotAttempted(
                f"the form shows the price as {seen.get('price')!r}, not {price}"
            )

    def commit(self, client, pause) -> None:
        """Press through the form's own steps. Called only inside the flow's commit bracket:
        anything raised from here on means a listing may exist, so raise `PublishUnverified` for a
        form that misbehaves — never `PublishNotAttempted`."""
        client.call_tool("browser_click", {"target": self.target("next"), "element": "Next"})
        pause(COMMIT_SETTLE_SEC)
        after = client.evaluate(self.fields_js) or {}
        if "publish" not in (after.get("marked") or []):
            raise PublishUnverified("the form moved on but offered no Publish button")
        client.call_tool("browser_click", {"target": self.target("publish"), "element": "Publish"})
        pause(COMMIT_SETTLE_SEC)
