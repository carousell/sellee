"""`_SCOPE_GUARDED` covers every Store accessor that takes a scoped id.

`ScopedStore.__getattr__` passes an unlisted method straight through to the Store, so the
allowlist fails **open**: an accessor that takes an item, thread or want id and is not named in
`_SCOPE_GUARDED` is reachable, unscoped, from a scoped pass. That is how ten of them came to be
unguarded at once — the hand-written scope tests each pick a method by name, so none of them
could notice an absence.

This reflects over `Store` instead: every public method taking one of the scoped id parameters
must either guard it or be listed in `_UNGUARDED_BY_DESIGN` with a reason. Adding an opt-out is
then a deliberate act with a justification attached, which is the property the dict alone cannot
have.
"""

from __future__ import annotations

import inspect

import pytest

from sellee.store import (
    _SCOPE_GUARDED,
    _SCOPE_MISS_EMPTY,
    _SCOPE_MISS_NONE,
    _SCOPE_MISS_NOTFOUND,
    _SCOPE_MISS_ZERO,
    Store,
)

# Parameter name -> the scope entity it references. `want_id` is included deliberately: the dict
# already guards six want-taking accessors, so leaving it out of the reflection would bless the
# next gap.
_ID_PARAMS = {"item_id": "item", "thread_id": "thread", "want_id": "want"}

# The (accessor, parameter) pairs that legitimately need no scope check, each stating why. An
# entry means "a scoped pass may pass any value in this position without reaching another seller
# entity or learning whether one exists".
#
# Keyed by parameter, never by accessor alone: an accessor-wide exemption also blesses whatever id
# that accessor grows next, which is the shape of gap this whole file exists to catch.
_UNGUARDED_BY_DESIGN = {
    ("set_crosslink_pushed", "item_id"): (
        "daemon-only: the crosslink lane writes it after the rail accepts a push, and no pass "
        "tier carries a tool that reaches it"
    ),
    ("add_scam_signature", "thread_id"): (
        "unvalidated provenance on a global signature bank, not a reference that reads or writes "
        "the thread — and the accessor has no missing-row behaviour to mirror, so a scope refusal "
        "would invent one and leak existence instead of hiding it"
    ),
    ("create_thread", "thread_id"): (
        "the new row's own natural key, not a reference to an existing thread — the insert "
        "refuses a duplicate and the prefix rule validates its shape"
    ),
}


def _unguarded_id_params(guarded: dict) -> dict:
    """{accessor: [unguarded id params]} for every public Store accessor, given an allowlist."""
    gaps: dict = {}
    for name, fn in inspect.getmembers(Store, inspect.isfunction):
        if name.startswith("_"):
            continue
        covered = {param for param, _kind in guarded.get(name, ())}
        missing = [
            param
            for param in inspect.signature(fn).parameters
            if param in _ID_PARAMS
            and param not in covered
            and (name, param) not in _UNGUARDED_BY_DESIGN
        ]
        if missing:
            gaps[name] = missing
    return gaps


def test_every_id_taking_accessor_is_guarded_or_deliberately_exempt() -> None:
    gaps = _unguarded_id_params(_SCOPE_GUARDED)
    assert not gaps, (
        "these Store accessors take a scoped id but are not in _SCOPE_GUARDED, so a scoped pass "
        "reaches them unscoped — guard them, or add a justified entry to _UNGUARDED_BY_DESIGN:\n"
        + "\n".join(f"  {name}({', '.join(params)})" for name, params in sorted(gaps.items()))
    )


def test_the_reflection_actually_fails_when_an_entry_is_removed() -> None:
    """A reflective guard that passes vacuously is worse than none: prove it reports a gap it is
    given, for every accessor this ticket added — not a sample of them."""
    for name in (
        "update_item",
        "record_listing_url",
        "record_checkout",
        "get_floor",
        "set_floor",
        "get_budget",
        "retract_detect_scam",
        "create_thread",
    ):
        pruned = {k: v for k, v in _SCOPE_GUARDED.items() if k != name}
        assert name in _unguarded_id_params(pruned), name


def test_an_exemption_does_not_extend_to_an_accessors_other_ids(monkeypatch) -> None:
    """The exemptions are keyed by parameter, so an exempt accessor that grows a second id is
    still reported. Keyed by accessor alone, this gap would be blessed silently."""

    def set_crosslink_pushed(self, item_id: str, thread_id: str, urls_json: str) -> None: ...

    monkeypatch.setattr(Store, "set_crosslink_pushed", set_crosslink_pushed)
    assert _unguarded_id_params(_SCOPE_GUARDED) == {"set_crosslink_pushed": ["thread_id"]}


def test_the_reflection_sees_a_newly_added_accessor(monkeypatch) -> None:
    """The gap this closes is a *future* accessor, so pin that an unlisted one is reported."""

    def touch_item(self, item_id: str) -> None: ...

    monkeypatch.setattr(Store, "touch_item", touch_item, raising=False)
    assert _unguarded_id_params(_SCOPE_GUARDED) == {"touch_item": ["item_id"]}


def test_an_exempt_parameter_does_not_exempt_the_whole_accessor() -> None:
    """create_thread's thread_id is exempt; its item_id and want_id are not."""
    pruned = {k: v for k, v in _SCOPE_GUARDED.items() if k != "create_thread"}
    assert sorted(_unguarded_id_params(pruned)["create_thread"]) == ["item_id", "want_id"]


@pytest.mark.parametrize("name", sorted(_SCOPE_GUARDED))
def test_every_guarded_accessor_declares_what_a_miss_answers(name) -> None:
    """`_deny` looks the accessor up in exactly one of these; a guarded accessor missing from all
    four would raise KeyError at denial time — a scope check that crashes instead of hiding."""
    categories = [
        name in _SCOPE_MISS_NONE,
        name in _SCOPE_MISS_EMPTY,
        name in _SCOPE_MISS_ZERO,
        name in _SCOPE_MISS_NOTFOUND,
    ]
    assert sum(categories) == 1, f"{name} must declare exactly one miss behaviour"
