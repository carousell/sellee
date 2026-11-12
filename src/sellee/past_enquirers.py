"""Which buyers who asked about an item hear that it changed, and the words they get.
Pure: the caller reads the store, decides when a change is live, and sends."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sellee.engines.negotiate import NOT_STANDING
from sellee.tools.reply import _refused

PRICE_DROP_TEXT = "The price of the {title} is now {new} (was {old})."
PHOTOS_TEXT = "I've added new photos of the {title}: {link}."
PRICE_DROP_AND_PHOTOS_TEXT = (
    "The price of the {title} is now {new} (was {old}), and I've added new photos: {link}."
)

# Currencies written with a bare "$"; any other code is written after the number.
_DOLLAR_CURRENCIES = frozenset({"USD", "SGD"})
_CURRENCY_CODE = re.compile(r"[A-Za-z]{3}")


@dataclass(frozen=True)
class ListingChange:
    """One change to an item that a marketplace now shows."""

    item_id: str
    market: str
    title: str
    currency: str | None
    old_price: float | None
    new_price: float | None
    photo_added: bool = False
    listing_url: str | None = None

    @property
    def price_dropped(self) -> bool:
        return (
            self.old_price is not None
            and self.new_price is not None
            and self.new_price < self.old_price
        )

    @property
    def photos_shown(self) -> bool:
        """A photo was added and there is a listing link to send with it."""
        return self.photo_added and bool(self.listing_url)

    @property
    def price_rose(self) -> bool:
        return (
            self.old_price is not None
            and self.new_price is not None
            and self.new_price > self.old_price
        )


@dataclass(frozen=True)
class PlannedUpdate:
    """One message for one thread. `in_msg_id` is the buyer message it follows, so sending it
    marks nothing newer as answered."""

    thread_id: str
    in_msg_id: str
    text: str


def plan_updates(
    change: ListingChange, threads: Iterable[Mapping], ledger: Mapping[str, Mapping]
) -> list[PlannedUpdate]:
    """One message per eligible thread. `threads` come from `Store.item_threads`; `ledger` is
    `negotiate_status(...)["buyers"]`."""
    text = _message(change)
    if text is None:
        return []
    return [
        PlannedUpdate(thread["thread_id"], thread["last_inbound_msg_id"], text)
        for thread in threads
        if _eligible(change, thread, ledger.get(thread["thread_id"]))
    ]


def told_about(change: ListingChange) -> tuple[bool, bool]:
    """What each planned message for `change` tells its buyer: (a price drop, new photos)."""
    if change.price_rose:
        return False, False
    return _price_shown(change), change.photos_shown


def _message(change: ListingChange) -> str | None:
    template = _template(change, PRICE_DROP_TEXT, PHOTOS_TEXT, PRICE_DROP_AND_PHOTOS_TEXT)
    if template is None:
        return None
    price = _price_shown(change)
    return template.format(
        title=change.title,
        new=format_price(change.new_price, change.currency or "") if price else "",
        old=format_price(change.old_price, change.currency or "") if price else "",
        link=change.listing_url,
    )


def _template(change: ListingChange, drop: str, photos: str, both: str) -> str | None:
    """The wording for `change`, or None. A price rise sends nothing, even with new photos."""
    if change.price_rose:
        return None
    if _price_shown(change):
        return both if change.photos_shown else drop
    return photos if change.photos_shown else None


def _price_shown(change: ListingChange) -> bool:
    # A price a buyer cannot read is never sent.
    return change.price_dropped and bool(_CURRENCY_CODE.fullmatch(change.currency or ""))


def _eligible(change: ListingChange, thread: Mapping, buyer: Mapping | None) -> bool:
    if thread["side"] != "sell" or thread["market"] != change.market:
        return False
    if thread["item_id"] != change.item_id:
        return False
    if _refused("sell", thread["status"], "followup"):
        return False  # the blocked-buyer close lands here too: it sets the thread `closed`
    last_in = thread["last_inbound_ts"]
    if last_in is None:
        return False
    # A buyer still waiting on an answer gets the answer, which carries the new price; a follow-up
    # sent first would mark their message answered.
    if thread["cursor_last_ts"] is None or thread["cursor_last_ts"] < last_in:
        return False
    # One update per buyer message. Only an earlier update counts: a nudge says nothing new, so it
    # never keeps a price drop from its buyer.
    if thread["last_update_ts"] is not None and thread["last_update_ts"] >= last_in:
        return False
    # A lowered minimum already owes this buyer a reply, which the reply lane will send.
    if thread["reply_owed_ts"] is not None:
        return False
    if buyer is None:
        return True
    if buyer.get("status") in NOT_STANDING:
        return False
    offer = buyer.get("highest_offer") or 0
    return not (offer > 0 and change.new_price is not None and offer >= change.new_price)


def format_price(amount: float | None, currency: str) -> str:
    """80 → "$80", 79.5 → "$79.50"; a non-dollar currency is written after the number."""
    cents = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    number = f"{cents:,.0f}" if cents == cents.to_integral_value() else f"{cents:,.2f}"
    code = currency.upper()
    return f"${number}" if code in _DOLLAR_CURRENCIES else f"{number} {code}"
