"""Typed accessors over the business database — the one writer for items, floors, and passes.

Every state change the LLM can cause lands here as an explicit function on the single write
connection, in a real transaction. Two disciplines are load-bearing:

  * The floor is confidential. It lives in its own table and is never returned by a read an
    LLM-facing tool can call — only the publish gate and (later) the engines load it. set_floor
    is the one hardened writer: it validates 0 < floor <= list_price (list price from the item
    record, never the caller), records provenance, refuses to let a `default` write clobber a
    seller value, requires force to replace one seller value with another, and never emits the
    value. The check and the write share one transaction so a race can't clobber a just-set
    seller floor with a default.

  * update_item is field-constrained: transcript-style fields don't exist here, listing_urls is
    written only by the publish path (never a hand-edit), and status moves only between draft and
    ready — the sale-state transitions belong to their owning flow, not a generic writer.

Passes are claimed single-flight: claim_queued_pass stamps running + started_ts in one
transaction, so two claimers never take the same row; a crash mid-pass is failed loudly by the
stale-running sweep, never silently re-run.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from sellee.db import Database
from sellee.store.browser import (
    BROWSER_HOLD_TTL_SEC,
    CONNECT_MODE_OPEN,
    CONNECT_MODE_PROBE,
    HOLD_SETUP,
    HOLD_SIGNIN,
    BrowserMixin,
)
from sellee.store.channel import ChannelMixin
from sellee.store.escalations import EscalationsMixin
from sellee.store.helpers import (
    BIND_NONCE_TTL_SEC,
    KNOWN_ADAPTERS,
    MAX_PHOTOS,
    QA_GLOBAL_ITEM,
    ClaimedPass,
    ItemNotFound,
    ItemRecord,
    StoreError,
    ThreadNotFound,
    ThreadSummary,
    WantNotFound,
    WantRecord,
    ask_notice_id,
    bind_nonce_live,
    ui_cache_is_stale,
)
from sellee.store.items import ItemsMixin
from sellee.store.negotiation import NegotiationMixin
from sellee.store.passes import PassesMixin
from sellee.store.scam import ScamMixin
from sellee.store.send import SendMixin
from sellee.store.settings import SettingsMixin
from sellee.store.survey import SurveyMixin
from sellee.store.threads import ThreadsMixin
from sellee.store.wants import WantsMixin

# The public surface of sellee.store: the names the rest of the tree (and the tests) import
# from the package. Some are defined here, some in helpers.py — where a name lives inside
# the package is not part of the contract, this list is.
__all__ = [
    "BIND_NONCE_TTL_SEC",
    "BROWSER_HOLD_TTL_SEC",
    "HOLD_SETUP",
    "HOLD_SIGNIN",
    "CONNECT_MODE_OPEN",
    "CONNECT_MODE_PROBE",
    "KNOWN_ADAPTERS",
    "MAX_PHOTOS",
    "QA_GLOBAL_ITEM",
    "ClaimedPass",
    "ItemNotFound",
    "Scope",
    "ScopedStore",
    "Store",
    "StoreError",
    "ThreadNotFound",
    "ask_notice_id",
    "bind_nonce_live",
    "ui_cache_is_stale",
]


class Store(
    ItemsMixin,
    BrowserMixin,
    ThreadsMixin,
    WantsMixin,
    NegotiationMixin,
    SendMixin,
    EscalationsMixin,
    ScamMixin,
    PassesMixin,
    ChannelMixin,
    SettingsMixin,
    SurveyMixin,
):
    """Typed access to sellee.db, serialized behind the single write connection.

    One class, one write connection, one lock. The accessors are split by domain across
    sellee/store/*.py as mixins — an organizational split only; every accessor is still a
    method on this one object, which is what ScopedStore's name-keyed delegation needs.
    """

    def __init__(self, db: Database):
        self._db = db


@dataclass(frozen=True)
class Scope:
    """The entity scope a headless pass was spawned with: the threads it may touch plus their
    owning items and wants. Attended sessions run unscoped (Session.scope is None)."""

    thread_ids: frozenset = frozenset()
    item_ids: frozenset = frozenset()
    want_ids: frozenset = frozenset()

    @classmethod
    def of(cls, *, threads=(), items=(), wants=()) -> Scope:
        return cls(frozenset(threads), frozenset(items), frozenset(wants))

    def allows(self, kind: str, value) -> bool:
        # An unset optional id is not an out-of-scope reference — it is simply not set. Empty
        # counts as unset alongside None: it is how a keyword-only id defaults, and it matches
        # no row anywhere, so it can neither leak nor reach another entity.
        if value is None or value == "":
            return True
        return (
            value
            in {
                "thread": self.thread_ids,
                "item": self.item_ids,
                "want": self.want_ids,
            }[kind]
        )

    def to_json(self) -> dict:
        return {
            "thread_ids": sorted(self.thread_ids),
            "item_ids": sorted(self.item_ids),
            "want_ids": sorted(self.want_ids),
        }

    @classmethod
    def from_json(cls, data: dict) -> Scope:
        return cls.of(
            threads=data.get("thread_ids", ()),
            items=data.get("item_ids", ()),
            wants=data.get("want_ids", ()),
        )


# Accessors that take a scoped id. For a scoped session every listed id argument must be in
# scope, or the call answers exactly as it would for a row that does not exist — an out-of-scope
# id must be indistinguishable from an absent one, so scope never leaks existence. Each entry is
# (name -> ((param, kind), ...)); later plans extend it as engine/mutation accessors land.
#
# ScopedStore.__getattr__ passes an unlisted method straight through, so an accessor that takes
# an id and is missing from here is unguarded. tests/guard/test_scope_guard.py reflects over
# Store to catch that, with an explicit opt-out set for the accessors that need no check.
_SCOPE_GUARDED = {
    "get_item": (("item_id", "item"),),
    "update_item": (("item_id", "item"),),
    "record_listing_url": (("item_id", "item"),),
    "get_floor": (("item_id", "item"),),
    "set_floor": (("item_id", "item"),),
    "get_budget": (("want_id", "want"),),
    "record_checkout": (("item_id", "item"), ("thread_id", "thread")),
    "retract_detect_scam": (("thread_id", "thread"),),
    # Not thread_id: that is the new row's own natural key, not a reference to an existing
    # thread (a duplicate is refused by the insert, and the prefix rule validates its shape).
    "create_thread": (("item_id", "item"), ("want_id", "want")),
    "set_photo_uploads": (("item_id", "item"),),
    "archive_listing_url": (("item_id", "item"),),
    # Creating an item passes None, which `allows` treats as unset rather than out of scope — so an
    # adoption that mints its own item is never refused.
    "adopt_discovered_listing": (("item_id", "item"),),
    "get_thread": (("thread_id", "thread"),),
    "get_thread_messages": (("thread_id", "thread"),),
    "append_thread_message": (("thread_id", "thread"),),
    "record_inbound": (("thread_id", "thread"),),
    "qa_add": (("item_id", "item"),),
    "qa_search": (("item_id", "item"),),
    "update_thread": (("thread_id", "thread"),),
    "hold_thread": (("thread_id", "thread"),),
    "release_thread": (("thread_id", "thread"),),
    "escalate": (("thread_id", "thread"),),
    "checkout_floor_gate": (("item_id", "item"),),
    "reserve_reply": (("thread_id", "thread"),),
    "commit_reply": (("thread_id", "thread"),),
    "record_manual_reply": (("thread_id", "thread"),),
    "get_want": (("want_id", "want"),),
    "update_want": (("want_id", "want"),),
    "cancel_want": (("want_id", "want"),),
    "negotiate_offer": (("item_id", "item"), ("thread_id", "thread")),
    "negotiate_status": (("item_id", "item"),),
    "negotiate_confirm_bid": (("item_id", "item"), ("thread_id", "thread")),
    "negotiate_confirm_sold": (("item_id", "item"), ("thread_id", "thread")),
    "negotiate_release": (("item_id", "item"),),
    "set_budget": (("want_id", "want"),),
    "buyer_negotiate_seed": (("want_id", "want"), ("thread_id", "thread")),
    "buyer_negotiate_open": (("want_id", "want"), ("thread_id", "thread")),
    "buyer_negotiate_reply": (("want_id", "want"), ("thread_id", "thread")),
    "buyer_negotiate_accept": (("want_id", "want"), ("thread_id", "thread")),
    "buyer_negotiate_walk": (("want_id", "want"), ("thread_id", "thread")),
}

# What a guarded accessor does when an id is out of scope: mirror the accessor's own
# missing-row behavior so the two are indistinguishable. Reads that return None on a missing
# row return None; the transcript read returns []; a counting delete returns 0; row-required
# writers raise the same NotFound.
_SCOPE_MISS_NONE = frozenset({"get_item", "get_thread", "get_want", "get_floor", "get_budget"})
_SCOPE_MISS_EMPTY = frozenset({"get_thread_messages", "qa_search"})
_SCOPE_MISS_ZERO = frozenset({"retract_detect_scam"})
_SCOPE_MISS_NOTFOUND = {
    "set_photo_uploads": ("item", ItemNotFound),
    "update_item": ("item", ItemNotFound),
    "record_listing_url": ("item", ItemNotFound),
    "set_floor": ("item", ItemNotFound),
    "record_checkout": ("item", ItemNotFound),
    "create_thread": ("item", ItemNotFound),
    "archive_listing_url": ("item", ItemNotFound),
    "adopt_discovered_listing": ("item", ItemNotFound),
    "append_thread_message": ("thread", ThreadNotFound),
    "record_inbound": ("thread", ThreadNotFound),
    "qa_add": ("item", ItemNotFound),
    "update_thread": ("thread", ThreadNotFound),
    "hold_thread": ("thread", ThreadNotFound),
    "release_thread": ("thread", ThreadNotFound),
    "escalate": ("thread", ThreadNotFound),
    "checkout_floor_gate": ("item", ItemNotFound),
    "reserve_reply": ("thread", ThreadNotFound),
    "commit_reply": ("thread", ThreadNotFound),
    "record_manual_reply": ("thread", ThreadNotFound),
    "update_want": ("want", WantNotFound),
    "cancel_want": ("want", WantNotFound),
    "negotiate_offer": ("item", ItemNotFound),
    "negotiate_status": ("item", ItemNotFound),
    "negotiate_confirm_bid": ("item", ItemNotFound),
    "negotiate_confirm_sold": ("item", ItemNotFound),
    "negotiate_release": ("item", ItemNotFound),
    "set_budget": ("want", WantNotFound),
    "buyer_negotiate_seed": ("want", WantNotFound),
    "buyer_negotiate_open": ("want", WantNotFound),
    "buyer_negotiate_reply": ("want", WantNotFound),
    "buyer_negotiate_accept": ("want", WantNotFound),
    "buyer_negotiate_walk": ("want", WantNotFound),
}


_NOT_FOUND_BY_KIND = {
    "item": ItemNotFound,
    "thread": ThreadNotFound,
    "want": WantNotFound,
}


class ScopedStore:
    """A scope-aware view over a Store. Unscoped (scope=None) it is a transparent pass-through —
    attended sessions hold full scope.

    Scoped, it enforces the entity scope at the accessors named in `_SCOPE_GUARDED`, answering an
    out-of-scope id exactly as a missing row, and filters list accessors to the scope rather than
    rejecting them. **Enforcement is opt-in, not the default**: an accessor absent from that dict
    is passed through unguarded, so adding one that takes an item, thread or want id means adding
    it there too — `tests/guard/test_scope_guard.py` reflects over `Store` to enforce exactly that,
    with an explicit opt-out set for the accessors that legitimately need no check."""

    def __init__(self, store: Store, scope: Scope | None = None):
        self._store = store
        self._scope = scope

    # List reads are filtered to the scope (a scoped pass enumerates only its own entities).
    def list_threads(
        self, side: str | None = None, status: str | None = None
    ) -> list[ThreadSummary]:
        rows = self._store.list_threads(side=side, status=status)
        if self._scope is None:
            return rows
        return [r for r in rows if r["thread_id"] in self._scope.thread_ids]

    def list_items(self, status: str | None = None) -> list[ItemRecord]:
        rows = self._store.list_items(status=status)
        if self._scope is None:
            return rows
        return [r for r in rows if r["id"] in self._scope.item_ids]

    def list_wants(self, status: str | None = None) -> list[WantRecord]:
        rows = self._store.list_wants(status=status)
        if self._scope is None:
            return rows
        return [r for r in rows if r["want_id"] in self._scope.want_ids]

    def __getattr__(self, name: str):
        target = getattr(self._store, name)
        spec = _SCOPE_GUARDED.get(name)
        scope = self._scope
        if scope is None or spec is None:
            return target
        sig = inspect.signature(target)

        def guarded(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            for param, kind in spec:
                if not scope.allows(kind, bound.arguments.get(param)):
                    return self._deny(name, bound.arguments, param, kind)
            return target(*args, **kwargs)

        return guarded

    def _deny(self, name: str, arguments: dict, param: str, kind: str):
        if name in _SCOPE_MISS_NONE:
            return None
        if name in _SCOPE_MISS_EMPTY:
            return []
        if name in _SCOPE_MISS_ZERO:
            return 0
        # The id that actually failed, not the accessor's first — an accessor taking two ids
        # would otherwise name a parameter the caller never set.
        _default_kind, exc = _SCOPE_MISS_NOTFOUND[name]
        raise _NOT_FOUND_BY_KIND.get(kind, exc)(f"no {kind} with id {arguments.get(param)!r}")
