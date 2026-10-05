"""Craigslist's read artifacts, run as the JavaScript they actually are.

Skipped without node on PATH. Two things are worth the exception here, and neither is a selector —
selectors can only be proven against the live site.

**The escaping.** These artifacts are Python string literals containing JavaScript regexes, so every
`\\s` and `\\/` has to survive one layer of quoting to reach the page intact. A broken escape is a
`SyntaxError` in the browser, which surfaces as a market that reports itself unreadable forever.

**The fail-closed paths.** Each of these three artifacts has one answer it must never give on a page
it did not understand, and each wrong answer is silent and expensive:

  * `LOGIN_JS` must not say `logged_out` — that tells a signed-in seller to re-authenticate and
    stops their market.
  * `MY_LISTINGS_JS` must not say `{listings: []}` — that closes the ask-once survey as "nothing
    listed" and nothing ever asks the seller again.
  * `LISTING_DETAIL_JS` must not say `{active: false}` — that is terminal in `adopt.py` and drops
    the seller's accepted listing for good.

An empty document stands in for every page shape we have not seen, which is the case that matters.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from sellee.browser.markets.craigslist import (
    CONVERSATION_TAIL_JS,
    CONVERSATIONS_LIST_JS,
    LISTING_DETAIL_JS,
    LOGIN_JS,
    MY_LISTINGS_JS,
)

node_binary = shutil.which("node")

pytestmark = pytest.mark.skipif(
    node_binary is None, reason="needs node on PATH to run the artifacts"
)

# A document that answers every query with nothing — the shape of any page we do not recognise.
_DOM_STUB = """
const __nodes = __NODES__;
const __match = (sel) => {
  const out = [];
  Object.keys(__nodes).forEach((key) => {
    if (String(sel).split(',').map((s) => s.trim()).includes(key)) {
      __nodes[key].forEach((n) => out.push(n));
    }
  });
  return out;
};
const __node = (spec) => ({
  tagName: ((spec.attrs || {}).tag || 'div').toUpperCase(),
  disabled: false,
  className: (spec.attrs || {}).class || '',
  innerText: spec.innerText || '',
  href: spec.href || '',
  textContent: spec.textContent || '',
  getAttribute: (name) => (spec.attrs || {})[name] || null,
  querySelectorAll: (sel) => __match(sel),
  querySelector: (sel) => __match(sel)[0] || null,
  closest: () => null,
  parentElement: null,
  cloneNode: () => __node(spec),
  remove: () => {},
});
globalThis.document = {
  visibilityState: 'visible',
  body: { innerText: __BODY__ },
  querySelector: (sel) => __match(sel)[0] || null,
  querySelectorAll: (sel) => __match(sel),
};
"""


def _run(
    artifact: str,
    nodes: dict | None = None,
    body: str = "",
    rows: dict | None = None,
) -> object:
    """Evaluate one artifact against a stubbed document and return its answer.

    `rows` repeats a selector's node N times, for the cases where the *count* is the thing under
    test — Gmail rendering one conversation as two rows, for one.
    """
    spec = {
        key: [
            {
                "innerText": n.get("text", ""),
                "href": n.get("href", ""),
                "attrs": n.get("attrs", {}),
            }
        ]
        * (rows or {}).get(key, 1)
        for key, n in (nodes or {}).items()
    }
    stub = _DOM_STUB.replace("__NODES__", json.dumps(spec)).replace("__BODY__", json.dumps(body))
    stub = stub.replace(
        "const __node = (spec) =>",
        "const __mk = (spec) =>",
    )
    # Materialise the stub nodes through the factory so each has the element shape.
    program = (
        stub
        + """
Object.keys(__nodes).forEach((k) => { __nodes[k] = __nodes[k].map(__mk); });
const __answer = ("""
        + artifact
        + """)();
process.stdout.write(JSON.stringify(__answer === undefined ? null : __answer));
"""
    )
    done = subprocess.run(
        [node_binary, "-e", program], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout or "null")


# --- the artifacts parse at all ----------------------------------------------------------------


@pytest.mark.parametrize(
    "artifact",
    [LOGIN_JS, CONVERSATIONS_LIST_JS, CONVERSATION_TAIL_JS, LISTING_DETAIL_JS, MY_LISTINGS_JS],
)
def test_every_artifact_is_valid_javascript(artifact) -> None:
    """A broken escape is a SyntaxError in the page, which the lane can only report as blindness."""
    done = subprocess.run(
        [
            node_binary,
            "-e",
            f"const f = ({artifact}); if (typeof f !== 'function') process.exit(9);",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr


# --- there is no inbox, and the artifacts say so ------------------------------------------------


def test_the_conversation_list_is_healthily_empty() -> None:
    """An empty list clears the read lane's blind counter; an `{error}` would accumulate toward a
    notice telling the seller to go and look at a page that never holds anything."""
    answer = _run(CONVERSATIONS_LIST_JS)
    assert answer["conversations"] == []
    assert "error" not in answer
    assert "no on-site inbox" in answer["reason"]


def test_the_conversation_tail_abstains_rather_than_claiming_silence() -> None:
    """`null` means "could not read"; `[]` would mean "nobody said anything", which is a different
    and false claim about a buyer."""
    assert _run(CONVERSATION_TAIL_JS) is None


# --- the three fail-closed answers --------------------------------------------------------------


def test_the_login_probe_never_guesses_logged_out() -> None:
    """The expensive wrong answer: it tells a signed-in seller to re-authenticate and stops the
    market. An unrecognised page must abstain."""
    answer = _run(LOGIN_JS, body="something we have never seen")
    assert answer["state"] == "unknown"
    assert answer["password_field"] is False


def test_the_login_probe_reads_the_real_signed_out_page() -> None:
    """Captured live: the signed-out account page carries a password field inside a form posting to
    the accounts login endpoint."""
    answer = _run(
        LOGIN_JS,
        nodes={
            'input[type="password"]': {},
            "form": {"attrs": {"action": "https://accounts.craigslist.org/login"}},
        },
    )
    assert answer["state"] == "logged_out"


def test_the_login_probe_reads_a_session_from_a_logout_control() -> None:
    answer = _run(LOGIN_JS, nodes={"a": {"attrs": {"href": "/logout"}}})
    assert answer["state"] == "logged_in"


def test_a_password_field_alone_is_not_proof_of_being_signed_out() -> None:
    """Without the accounts form around it, a password box could be anything — a change-password
    panel on a signed-in page, for one."""
    answer = _run(LOGIN_JS, nodes={'input[type="password"]': {}})
    assert answer["state"] == "unknown"


def test_the_listings_read_errors_rather_than_reporting_an_empty_inventory() -> None:
    """`record_survey_result(market, [])` closes the ask-once survey with found=0 and nothing ever
    asks again, so an unparsed page must not answer with an empty list."""
    answer = _run(MY_LISTINGS_JS, body="signed out, or a page we do not know")
    assert "error" in answer
    assert "listings" not in answer


def test_an_account_with_nothing_posted_is_read_as_empty_not_unreadable() -> None:
    """The other half of that rule, and the half that bit first.

    A signed-in account with nothing up carries no table, no rows and no manage forms — it says
    "no postings" in words (captured live 2026-09-09). Read as unparseable, that answer costs one
    of only five survey attempts per tick, so a brand-new seller had their craigslist survey
    abandoned permanently within about five minutes of connecting, having never been asked
    anything. Absence of rows is not absence of an answer.
    """
    answer = _run(
        MY_LISTINGS_JS,
        body="craigslist > home of someone\npostings drafts searches settings\nno postings\n",
    )
    assert "error" not in answer
    assert answer["listings"] == []
    assert answer["active_count"] == 0
    assert answer["truncated"] is False
    assert answer["empty_stated"] is True


def test_the_listing_detail_abstains_on_a_page_it_cannot_read() -> None:
    """A falsy `active` is terminal in adopt.py — it drops the seller's accepted listing as "no
    longer for sale". A half-rendered page must answer null instead."""
    assert _run(LISTING_DETAIL_JS) is None


def test_the_listing_detail_reports_a_removed_post_as_gone() -> None:
    """Craigslist serves 410 with this banner for a deleted *and* an expired post — and expiry does
    not mean sold, which is why the caller has to ask rather than infer."""
    answer = _run(LISTING_DETAIL_JS, nodes={"#has_been_removed": {}})
    assert answer["active"] is False
    assert answer["availability"] == "removed"


# --- the mail view: empty is an answer, not a failure -------------------------------------------


# --- an opened conversation, and the control a reply must never use ------------------------------


# --- the account page's real table, and the invariant that stops a duplicate --------------------


def _posting_row(*, href: str, shown: str, status: str = "Active", post_id: str = "7963324125"):
    """The cells of one `tr.posting-row`, as captured live on 2026-09-09."""
    return {
        "tr.posting-row": {},
        "td.postingID": {"text": post_id},
        "td.title a[href]": {"text": shown, "href": href, "attrs": {"href": href}},
        "td.status": {"text": status},
        "td.expdate": {"text": "29 days"},
        "td.posteddate": {"text": "09 Sep 2026 04:03"},
        "td.areacat": {"text": "sfo - sfc household items - by owner"},
    }


CANONICAL = "https://www.craigslist.org/view/d/san-francisco-desk-lamp/kYA5WSQXhG8jRAGTiue3Bd"


def test_the_reported_id_is_the_one_the_stored_url_yields() -> None:
    """The invariant, and the most expensive thing in this integration to get wrong.

    A posting has two addresses carrying two different ids — canonical `/view/d/<slug>/<token>` and
    an older `<city>…/<numeric>.html`. The survey recognises an already-managed posting by comparing
    the id it reads against the id `LISTING_ID_PATTERN` extracts from the item's *stored* URL, so
    those two have to be the same id space.

    On the first live run they were not: the publish stored the older URL and the survey reported
    the canonical token, so the dedup found nothing and the seller's own managed posting was adopted
    a second time — a duplicate item, and a duplicate relist behind it. Both safety nets missed it,
    because the title net deliberately skips items that already hold a URL on this market.
    """
    from sellee.browser import reconcile
    from sellee.browser.markets.craigslist import LISTING_ID_PATTERN, MY_LISTINGS_JS

    answer = _run(MY_LISTINGS_JS, nodes=_posting_row(href=CANONICAL, shown="Desk lamp - $15"))
    row = answer["listings"][0]
    assert row["listing_id"] == reconcile.listing_id(row["url"], LISTING_ID_PATTERN)


def test_the_row_carries_craigslists_own_post_id_too() -> None:
    """Not the join key, but the one id present on every surface the posting has — the account
    page's own column, and the listing page's "post id:" line."""
    from sellee.browser.markets.craigslist import MY_LISTINGS_JS

    answer = _run(MY_LISTINGS_JS, nodes=_posting_row(href=CANONICAL, shown="Desk lamp - $15"))
    assert answer["listings"][0]["post_id"] == "7963324125"


def test_the_price_craigslist_appends_to_the_title_is_not_kept_as_the_title() -> None:
    """The link text is "<title> - $<price>". Kept, the price is stored inside the item's name and
    travels onto every other marketplace it is listed to — the ask read "…works fine - $15 — $15".
    """
    from sellee.browser.markets.craigslist import MY_LISTINGS_JS

    answer = _run(
        MY_LISTINGS_JS,
        nodes=_posting_row(href=CANONICAL, shown="Desk lamp, grey — works fine - $15"),
    )
    row = answer["listings"][0]
    assert row["title"] == "Desk lamp, grey — works fine"
    assert row["price"] == 15
    assert row["price_text"] == "$15"


def test_expiry_is_read_because_nothing_else_will_ever_mention_it() -> None:
    """Craigslist has no sold state and a free posting dies in weeks with no signal on its public
    page. This column is the only warning that a managed listing is about to stop existing."""
    from sellee.browser.markets.craigslist import MY_LISTINGS_JS

    answer = _run(MY_LISTINGS_JS, nodes=_posting_row(href=CANONICAL, shown="Desk lamp - $15"))
    assert answer["listings"][0]["expires_in"] == "29 days"


def test_a_posting_that_is_not_active_is_dropped_not_offered() -> None:
    """Status is its own cell, so this is a fact rather than a search for words that might appear
    anywhere in the row. Relisting something the seller already took down is worse than missing it.
    """
    from sellee.browser.markets.craigslist import MY_LISTINGS_JS

    answer = _run(
        MY_LISTINGS_JS,
        nodes=_posting_row(href=CANONICAL, shown="Desk lamp - $15", status="Expired"),
    )
    assert answer["listings"] == []
    assert answer["dropped"] == 1
    assert answer["active_count"] == 1


# --- the send, and the toast that must not be misread -------------------------------------------


# --- the mailbox login probe --------------------------------------------------------------------
