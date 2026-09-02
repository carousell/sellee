"""Mechanics a market's publish choreography may borrow: typed input, dropdowns, read-backs.

Nothing here decides an order — a create form's sequence belongs to the market whose form it is
(`markets/publishing.py`). These are the pieces two markets would otherwise write twice, and each
one is opt-in: a market composes what its own form needs and ignores the rest. A helper's failure
is always a refusal (`PublishNotAttempted`), because every one of them runs before anything
irreversible.
"""

from __future__ import annotations

from sellee.browser.client import BrowserError
from sellee.browser.markets.publishing import STEP_SETTLE_SEC, PublishNotAttempted


def type_text(client, target: str, step: str, text) -> None:
    """Type into one field the way a person would.

    Never `value =`: the form listens for real input, and a value set from script leaves React
    holding the old one — which publishes an empty listing.
    """
    try:
        client.call_tool(
            "browser_type",
            {
                "target": target,
                "element": f"the {step} field",
                "text": str(text),
                "submit": False,
            },
        )
    except BrowserError as exc:
        raise PublishNotAttempted(f"could not fill {step}: {exc}", retryable=True) from exc


def bare_price(price) -> str:
    """A price as a price field should receive it: no grouping separators.

    The field formats what it is given, and a grouped "1,299" has been read as 1 by more than one
    marketplace form.
    """
    if isinstance(price, (int, float)) and price == int(price):
        return f"{price:.0f}"
    return "" if price is None else str(price)


def pick_option_by_name(
    client,
    *,
    market: str,
    step: str,
    target: str,
    option_target: str,
    options_js: str,
    wanted: str,
    pause,
    settle: float = STEP_SETTLE_SEC,
) -> None:
    """Open one dropdown and pick the option that shows `wanted`.

    `options_js` is the market's artifact with the wanted text already baked in; it marks the
    matching row so `option_target` can be clicked. An unsatisfiable dropdown is a refusal:
    carrying on would press Publish against a form that rejects it.
    """
    try:
        client.call_tool("browser_click", {"target": target, "element": f"the {step} dropdown"})
        pause(settle)
        answer = client.evaluate(options_js) or {}
        if not answer.get("chosen"):
            raise PublishNotAttempted(
                f"{market} offers no {step} called {wanted!r} "
                f"(it offers {(answer.get('options') or [])[:8]})"
            )
        client.call_tool("browser_click", {"target": option_target, "element": f"the {step}"})
        pause(settle)
    except PublishNotAttempted:
        raise
    except BrowserError as exc:
        raise PublishNotAttempted(f"could not choose a {step}: {exc}", retryable=True) from exc


def check_readback(client, readback_js: str, item: dict) -> None:
    """Refuse unless the form holds what the item said.

    The title must match exactly; the price is compared on its digits alone, because the field is
    free to redraw "65" as "$65". A field that truncated or dropped its input otherwise becomes a
    live listing the seller has to find and fix.
    """
    seen = client.evaluate(readback_js) or {}
    title = (item.get("title") or "").strip()
    got = (seen.get("title") or "").strip()
    if title and got != title:
        raise PublishNotAttempted(f"the form shows the title as {got!r}, not {title!r}")
    price = item.get("list_price")
    digits = "".join(ch for ch in str(seen.get("price") or "") if ch.isdigit())
    if isinstance(price, (int, float)) and digits and int(digits) != int(price):
        raise PublishNotAttempted(f"the form shows the price as {seen.get('price')!r}, not {price}")
