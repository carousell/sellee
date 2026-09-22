"""A marketplace that has told us to stop.

Durable on purpose, and that is the whole design decision: every other brake in the read lane is an
in-process counter documented as erring toward reading MORE after a restart, which is right for a
fault of ours and exactly inverted for a marketplace that has warned the account. A wall makes a
restart likely — the seller is being told something is wrong — so a restart must not be what
un-brakes it.
"""

from __future__ import annotations

from sellee.store import StoreError  # noqa: F401  (kept for symmetry with the other store tests)

_MARKET = "fb"


def _notices(store):
    """Every notice queued so far — the read does not drain, so these accumulate."""
    return [n["text"] for n in store.claim_queued_notices(10)]


def test_a_market_with_no_row_is_free_to_drive(store) -> None:
    assert store.market_block(_MARKET) is None
    assert store.blocked_markets() == []


def test_a_block_holds_until_its_window_closes(store) -> None:
    store.block_market(_MARKET, "automation", ttl_sec=100.0, now=1000.0)

    assert store.market_block(_MARKET, now=1050.0)["cause"] == "automation"
    assert store.blocked_markets(now=1050.0) == [_MARKET]
    assert store.market_block(_MARKET, now=1101.0) is None
    assert store.blocked_markets(now=1101.0) == []


def test_an_indefinite_block_never_expires(store) -> None:
    """`restricted` has nothing a button can do, so it decays for nobody."""
    store.block_market(_MARKET, "restricted", ttl_sec=None, now=1000.0)

    assert store.market_block(_MARKET, now=10**9)["cause"] == "restricted"


def test_blocking_again_renews_and_counts_a_strike(store) -> None:
    store.block_market(_MARKET, "automation", ttl_sec=100.0, now=1000.0)
    store.block_market(_MARKET, "automation", ttl_sec=500.0, now=1050.0)

    block = store.market_block(_MARKET, now=1400.0)
    assert block["strikes"] == 2
    assert block["expires_ts"] == 1550.0


def test_a_block_is_only_one_row_per_market(store) -> None:
    store.block_market(_MARKET, "automation", ttl_sec=100.0, now=1000.0)
    store.block_market(_MARKET, "automation", ttl_sec=100.0, now=1001.0)

    assert store.blocked_markets(now=1002.0) == [_MARKET]


def test_clearing_lets_the_market_be_driven_again(store) -> None:
    store.block_market(_MARKET, "automation", ttl_sec=None, now=1000.0)
    store.clear_market_block(_MARKET)

    assert store.market_block(_MARKET) is None


def test_clearing_a_market_that_was_never_blocked_is_fine(store) -> None:
    store.clear_market_block(_MARKET)


# --- telling the seller ---------------------------------------------------------------------------


def test_the_seller_is_told_once_per_incident(store) -> None:
    store.block_market(_MARKET, "automation", ttl_sec=100.0, now=1000.0)

    assert store.report_market_block_once(_MARKET, "Facebook has stopped me.") is True
    assert store.report_market_block_once(_MARKET, "Facebook has stopped me.") is False
    assert _notices(store) == ["Facebook has stopped me."]


def test_a_strike_on_the_same_incident_does_not_re_tell(store) -> None:
    store.block_market(_MARKET, "automation", ttl_sec=100.0, now=1000.0)
    store.report_market_block_once(_MARKET, "first")

    store.block_market(_MARKET, "automation", ttl_sec=100.0, now=1010.0)

    assert store.report_market_block_once(_MARKET, "second") is False
    assert _notices(store) == ["first"]


def test_a_new_cause_is_a_new_incident_and_is_told(store) -> None:
    """A block that became something else is a new thing to say, and a preserved `told_ts` would
    be what silences it."""
    store.block_market(_MARKET, "automation", ttl_sec=100.0, now=1000.0)
    store.report_market_block_once(_MARKET, "first")

    store.block_market(_MARKET, "checkpoint", ttl_sec=100.0, now=1010.0)

    assert store.report_market_block_once(_MARKET, "second") is True
    assert _notices(store) == ["first", "second"]


def test_a_block_that_expired_and_came_back_is_told_again(store) -> None:
    """The regression `told_ts` alone would have: a fresh incident silenced by the record of the
    last one."""
    store.block_market(_MARKET, "automation", ttl_sec=10.0, now=1000.0)
    store.report_market_block_once(_MARKET, "first")
    assert store.market_block(_MARKET, now=1100.0) is None  # it lapsed

    store.clear_market_block(_MARKET)  # what a recovery probe does before a fresh block
    store.block_market(_MARKET, "automation", ttl_sec=10.0, now=1100.0)

    assert store.report_market_block_once(_MARKET, "second") is True


def test_the_notice_carries_its_controls(store) -> None:
    store.block_market(_MARKET, "automation", ttl_sec=100.0, now=1000.0)
    store.report_market_block_once(_MARKET, "tap below", [("Check again", "fb:connectchk")])

    queued = store.claim_queued_notices(10)
    assert queued[0]["controls"] == [["Check again", "fb:connectchk"]]


def test_telling_a_market_that_is_not_blocked_queues_nothing(store) -> None:
    assert store.report_market_block_once(_MARKET, "nothing to say") is False
    assert _notices(store) == []


def test_strikes_survive_the_window_they_were_counted_in(store) -> None:
    """The escalation counts walls "with no clean probe in between", so a block that lapsed and
    was never cleared still carries what happened — otherwise a repeat offender restarts at the
    shortest window every time."""
    store.block_market(_MARKET, "automation", ttl_sec=10.0, now=1000.0)
    store.block_market(_MARKET, "automation", ttl_sec=10.0, now=1005.0)

    assert store.market_block(_MARKET, now=1100.0) is None  # the window lapsed
    assert store.market_block_strikes(_MARKET) == 2  # what happened did not


def test_a_clean_probe_resets_the_strikes(store) -> None:
    """Clearing is the one thing that means the market is genuinely fine again."""
    store.block_market(_MARKET, "automation", ttl_sec=10.0, now=1000.0)
    store.clear_market_block(_MARKET)

    assert store.market_block_strikes(_MARKET) == 0
