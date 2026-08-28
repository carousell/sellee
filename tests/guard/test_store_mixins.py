"""No two store mixins define the same attribute.

`Store` composes ten mixins from `sellee/store/*.py`, so an accessor defined in two of them is
resolved silently by the MRO: the first mixin in the bases list wins and the other body simply
never runs. Nothing else would fail — the tests exercising the losing definition go on passing
against the winner, which is usually near-identical. That is the one failure mode the split
introduced, and it is invisible without a check like this.

`Store` itself is allowed to define things (it owns `__init__`); the rule is only that no two
*mixins* collide.
"""

from __future__ import annotations

import collections

import pytest

from sellee.store import Store

_EXPECTED = {
    "ItemsMixin",
    "BrowserMixin",
    "ThreadsMixin",
    "WantsMixin",
    "NegotiationMixin",
    "SendMixin",
    "EscalationsMixin",
    "ScamMixin",
    "PassesMixin",
    "ChannelMixin",
    "SettingsMixin",
    "SurveyMixin",
}


def _mixins() -> list[type]:
    return [c for c in Store.__mro__ if c not in (Store, object)]


def _owners(mixins) -> dict:
    """attribute -> the mixins defining it. Dunders are skipped: every class body gets several
    for free, and under PEP 649 which of them are materialized depends on access order."""
    owners = collections.defaultdict(list)
    for cls in mixins:
        for name in vars(cls):
            if not (name.startswith("__") and name.endswith("__")):
                owners[name].append(cls.__name__)
    return owners


def test_the_mixins_are_all_here() -> None:
    """Guards the guard: if the composition were empty this file would pass vacuously."""
    assert {c.__name__ for c in _mixins()} == _EXPECTED


def test_no_attribute_is_defined_by_two_mixins() -> None:
    clashes = {name: who for name, who in _owners(_mixins()).items() if len(who) > 1}
    assert clashes == {}, f"shadowed by the MRO, one body never runs: {clashes}"


def test_a_shadowed_accessor_is_detected(monkeypatch) -> None:
    """The check above is only worth having if it fails when a shadow exists. Put `get_item` on a
    second mixin and it must be caught — the real thing this file is here to notice."""
    from sellee.store.threads import ThreadsMixin

    monkeypatch.setattr(ThreadsMixin, "get_item", lambda self, item_id: None, raising=False)
    clashes = {name: who for name, who in _owners(_mixins()).items() if len(who) > 1}
    assert "get_item" in clashes


def test_every_mixin_contributes_something() -> None:
    """A mixin whose methods all moved elsewhere should be deleted, not left in the bases list."""
    owned = collections.Counter()
    for who in _owners(_mixins()).values():
        owned.update(who)
    assert [c.__name__ for c in _mixins() if not owned[c.__name__]] == []


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_each_mixin_is_reachable_from_the_package(name) -> None:
    """Every mixin is exported from `sellee.store`, so the composition and the import block in
    `__init__.py` cannot drift apart unnoticed."""
    import sellee.store

    assert isinstance(getattr(sellee.store, name), type)
