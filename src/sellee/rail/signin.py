"""The seller's carousell.ai sign-in link: email, then the ship-from ZIP that lets buyers pay.
One mint for every caller, so each refuses the same links, since the URL hands over the account."""

from __future__ import annotations

from sellee.rail.client import RailError, RailToolError

# carousell.ai takes payments for sellers in the United States only, so only they need the sign-in.
CHECKOUT_REGION = "US"

# The rail's refusal for an account that is past sign-in. A result, not a failure.
_ALREADY_A_SELLER = "already a seller"


class OffBaseSigninLink(RailError):
    """The rail answered with a link that is not on the carousell.ai sign-in page."""


def _signin_base(web_base_url: str) -> str:
    return web_base_url.rstrip("/") + "/signin"


def mint_signin_url(rail, web_base_url: str) -> str | None:
    """A fresh sign-in link for the account `rail` holds the key to, or None once it is a seller.
    Raises RailError for anything else, OffBaseSigninLink included."""
    try:
        minted = rail.create_promotion_url()
    except RailToolError as exc:
        if _ALREADY_A_SELLER in str(exc):
            return None
        raise
    raw = minted.get("promotion_url")
    url = raw.strip() if isinstance(raw, str) else ""
    base = _signin_base(web_base_url)
    # Exact base or a real `?`/`/` boundary, as one unbroken run of printable characters: a bare
    # prefix would also accept `/signinfoo`, and a control character could forge a terminal line.
    on_base = url == base or url.startswith((base + "?", base + "/"))
    if not on_base or not url.isprintable() or any(ch.isspace() for ch in url):
        raise OffBaseSigninLink(
            "sign-in link did not come from the expected carousell.ai sign-in base"
        )
    return url
