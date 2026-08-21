"""The web tail's page never carries the attended token in a URL.

The token grants the whole product and never expires by itself, so it must stay out of the address
bar and out of the browser's history — that is the property SEC-2814 was about. The page gets it by
trading a one-shot ticket for it, then keeps it in `sessionStorage` so a refresh survives without
the token ever being in a URL.

This is a **content** guard, not a behavioural test: the page is browser JavaScript, and the tree
has no JS runtime to execute it in (stdlib-only runtime, no node test harness), so the only
coverage available below the level of a manual check is to pin the shape of the file. It is here
rather than beside the server tests because that is what it is — a scan asserting an invariant a
future edit could quietly undo, like the path-authority and stdlib-purity guards next to it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "src" / "sellee" / "data" / "tail.html"


def _page() -> str:
    return PAGE.read_text()


def test_the_page_never_writes_a_credential_into_a_url() -> None:
    """The regression to fear is someone "simplifying" the exchange back into `?token=`."""
    page = _page()
    # A token reaching location/history is the whole defect: both put it somewhere that persists.
    for pattern in (
        r"location\.href\s*=",
        r"location\.assign",
        r"location\.replace\s*\(",
        r"pushState\s*\([^)]*token",
        r"replaceState\s*\([^)]*token",
    ):
        assert not re.search(pattern, page), pattern
    # The token travels in a POST body to the exchange route, never as a query parameter on it.
    assert "control/tail-exchange" in page
    assert not re.search(r"tail-exchange\?[^\"']*(token|ticket)", page)


def test_the_ticket_is_read_from_the_fragment_and_scrubbed() -> None:
    """A fragment is never sent to the server, so the ticket stays out of the daemon's own logs;
    scrubbing it keeps the history entry clean once it has been traded."""
    page = _page()
    assert "location.hash" in page
    assert re.search(r"history\.replaceState\(\s*null,\s*\"\",", page)


def test_the_token_is_kept_in_session_storage_not_local_storage() -> None:
    """sessionStorage dies with the tab. localStorage would persist the token to disk for every
    later tab on this origin, which is a materially worse place to leave it."""
    page = _page()
    assert "sessionStorage" in page
    assert "localStorage" not in page


def test_a_refresh_can_recover_the_token() -> None:
    """The reason the storage is there at all: reloading is a reflex on a log page, and the ticket
    cannot serve a reload (single-use, and the fragment is gone by then)."""
    page = _page()
    assert "sessionStorage.setItem" in page
    assert "sessionStorage.getItem" in page


def test_a_revoked_token_stops_the_poll_instead_of_retrying_forever() -> None:
    """A page polling a 401 once a second is indistinguishable from an agent that has gone quiet —
    the failure mode is a reader trusting a tail that stopped following."""
    page = _page()
    assert "response.status === 401" in page
    assert "sessionStorage.removeItem" in page


def test_no_credential_is_explained_as_two_distinct_states() -> None:
    """Arriving with a ticket that no longer works and arriving without one at all are different
    situations. Telling someone who typed the address that their link expired is a lie about a
    link they never had, and it buries the one thing they need to do."""
    page = _page()
    assert "spent:" in page and "unlinked:" in page
    # both dead ends name the command that fixes them
    for reason in re.findall(r"^\s+(?:spent|unlinked):\s*\"(.+)\",$", page, re.M):
        assert "sellee logs --web" in reason, reason
    # and the two do not say the same thing
    reasons = re.findall(r"^\s+(?:spent|unlinked):\s*\"(.+)\",$", page, re.M)
    assert len(reasons) == 2 and reasons[0] != reasons[1]
