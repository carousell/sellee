"""Filling a marketplace form the way a person does — shared by the publisher and the editor.

Both drivers act on controls a market's JS artifact has marked, and both are watched by the same
instrumentation: per-field focus and input events feed a form's draft autosave and its abandonment
funnel. Everything here is about looking like the person the seller is — typed rather than set,
paced rather than instant, and stopped short when the marketplace is refusing the account.

What differs between creating a listing and changing one lives in the drivers: which controls,
which commit button, and whether a mistake after the commit is safe to repeat.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Callable

from sellee.browser.client import BrowserError

log = logging.getLogger(__name__)

# Between one form field and the next. Shorter than a step, because moving between fields is not
# the same act as waiting for a page to respond to one.
FIELD_SETTLE_SEC = 1.5
# How long the form is given to settle between steps, in seconds. A dropdown fetches its options.
STEP_SETTLE_SEC = 2.0
COMMIT_SETTLE_SEC = 6.0

# How much a settle may vary either side. The same fields in the same order at fixed millisecond
# pauses is nobody's way of filling a form; the point is variance, not slowness, so the average
# pause is unchanged.
JITTER = 0.4

# Selecting everything in the focused box before typing a replacement. A value already in the box
# is exactly what an edit form arrives with, and a per-character type appends to it.
_SELECT_ALL = "ControlOrMeta+a"

# The first number in a price reading, cents included, once thousands separators are gone.
_PRICE_RE = re.compile(r"\d+(?:\.\d+)?")


def sleep(seconds: float) -> None:
    time.sleep(random.uniform(seconds * (1 - JITTER), seconds * (1 + JITTER)))


def bare_price(price):
    """A price as it should be typed: bare digits for a whole number. A grouped "1,299" has been
    read as 1 by more than one marketplace form, so the field is left to format what it is given."""
    if isinstance(price, (int, float)) and not isinstance(price, bool) and price == int(price):
        return f"{price:.0f}"
    return price


def read_price(shown):
    """The number a form or page shows as a price, or None when it shows none.

    Parsed, not digit-joined: the page may prefix a currency ("SGD115", "S$120"), group thousands
    ("1,299"), or carry cents ("115.00"). Joining every digit read Carousell's own "115.00" as
    11500, which would have reported a correct edit as failed.
    """
    match = _PRICE_RE.search(str(shown or "").replace(",", ""))
    return float(match.group(0)) if match else None


def price_matches(shown, price) -> bool:
    """Whether a form or page shows this price. A reading with no number in it says nothing, so it
    is not a mismatch — the caller decides whether silence is acceptable."""
    seen = read_price(shown)
    if not isinstance(price, (int, float)) or isinstance(price, bool) or seen is None:
        return True
    return abs(seen - float(price)) < 0.005


def refuse_at_a_wall(client, adapter, refusal: Callable[[str], Exception]) -> None:
    """Stop before acting if the marketplace is refusing the account.

    `refusal` builds the caller's own "nothing was done, try again" exception: a wall is exactly
    the condition that clears on its own or by the seller, and nothing has been touched.

    A probe that will not run is not evidence of a wall — the form reads that follow report their
    own failures, and refusing on a failed probe would stop the work on a browser hiccup.
    """
    if not adapter.block_wall_js:
        return
    try:
        wall = str(client.evaluate(adapter.block_wall_js) or "")
    except BrowserError:
        return
    if wall:
        raise refusal(f"{adapter.market} is refusing the account ({wall}) — nothing was filled in")


def type_fields(
    client,
    target: Callable[[str], str],
    fields: list,
    marked,
    refusal: Callable[[str], Exception],
    pause,
    *,
    replace: bool = False,
) -> None:
    """Type each `(step, text)` into its marked control. Never `value =`: the form listens for real
    input, and a value set from script leaves React holding the old one.

    `replace` empties the box first with a real select-all and delete — an edit form arrives full,
    and typing onto a price of 150 would leave it reading 150120. An empty value is then the whole
    change: clearing a description is emptying its box and typing nothing. Without `replace` an
    empty value is skipped, because an untouched create-form box is already empty. The caller's
    read-back before committing is what catches a box that did not empty.
    """
    for step, text in fields:
        if step not in (marked or []):
            continue
        blank = text is None or text == ""
        if blank and not replace:
            continue
        try:
            if replace:
                client.call_tool("browser_click", {"target": target(step), "element": step})
                client.call_tool("browser_press_key", {"key": _SELECT_ALL})
                client.call_tool("browser_press_key", {"key": "Backspace"})
            if not blank:
                client.type_humanly(target(step), f"the {step} field", str(text))
        except BrowserError as exc:
            raise refusal(f"could not fill {step}: {exc}") from exc
        pause(FIELD_SETTLE_SEC)
