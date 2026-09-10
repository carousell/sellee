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
globalThis.location = __LOCATION__;
"""


def _run(
    artifact: str,
    nodes: dict | None = None,
    body: str = "",
    rows: dict | None = None,
    location: dict | None = None,
) -> object:
    """Evaluate one artifact against a stubbed document and return its answer.

    `rows` repeats a selector's node N times, for the cases where the *count* is the thing under
    test — Gmail rendering one conversation as two rows, for one. `location` stubs the page's own
    address, which the mailbox login probe reads: signed in and signed out are two different hosts.
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
    stub = (
        _DOM_STUB.replace("__NODES__", json.dumps(spec))
        .replace("__BODY__", json.dumps(body))
        .replace("__LOCATION__", json.dumps({"hostname": "", "hash": "", **(location or {})}))
    )
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


def test_the_mail_list_reads_gmails_own_words_for_an_empty_view() -> None:
    """The same lesson the craigslist account reader learned the hard way, applied before it can
    bite twice.

    Captured live 2026-09-09 from a real signed-in mailbox with no relay mail: the scoped search
    renders `[role="main"]` containing "No messages matched your search." Read as unparseable, an
    empty mailbox would climb the blind counter and tell the seller to check a mailbox that is
    simply empty — the mail equivalent of the survey abandonment.
    """
    from sellee.mail.gmail import message_list_js

    answer = _run(
        message_list_js(),
        nodes={'[role="main"]': {"text": "Conversations\nNo messages matched your search."}},
    )
    assert "error" not in answer
    assert answer["conversations"] == []
    assert answer["empty_stated"] is True


def test_the_mail_list_errors_on_a_view_it_cannot_read() -> None:
    """A view that is still loading, signed out, or a shape we have not seen is retried — the one
    thing it must not be is reported as "no buyers wrote"."""
    from sellee.mail.gmail import message_list_js

    answer = _run(message_list_js())
    assert "error" in answer
    assert "conversations" not in answer


def test_the_mail_list_never_carries_a_non_relay_subject_out_of_the_page() -> None:
    """The guard runs inside the page as well as in Python, and this is the difference between
    "we filtered it" and "it was never read": a subject line from the rest of the seller's mailbox
    does not enter this process at all.

    The row itself is now carried — as an **opaque id and nothing else**. That is not a weakening:
    it is how a *forwarded* craigslist thread arrives, from the buyer's own ordinary address, which
    no sender test can admit. What the row is gets decided by content when it is opened.
    """
    from sellee.mail.gmail import message_list_js

    answer = _run(
        message_list_js(),
        nodes={
            "tr.zA": {},
            "[email]": {"attrs": {"email": "colleague@thecarousell.com"}},
            "[data-legacy-thread-id]": {"attrs": {"data-legacy-thread-id": "1a08beef"}},
            ".bog": {"text": "Q3 board deck — confidential"},
        },
    )
    assert answer["blocked"] == 1
    assert answer["conversations"] == [
        {"provider_thread_id": "1a08beef", "verified": False, "unread": False}
    ]
    assert "confidential" not in json.dumps(answer)
    assert "colleague" not in json.dumps(answer)


def test_the_mail_list_keys_a_conversation_on_gmails_stable_id() -> None:
    """The regression guard for a bug that shipped in this file and would have been invisible.

    An earlier version returned `row.getAttribute('id')` as the message id. Measured live
    2026-09-10, the open thread's row was `id=":48"` — a Gmail widget id, reassigned per render.
    Keyed on that, `mail_relay_seen` matches nothing on the next read and every buyer message is
    folded in again on every tick: the buyer appears to repeat themselves and the agent answers
    each repeat.

    The stable id is `data-legacy-thread-id`, and it is reachable from the list row.
    """
    from sellee.mail.gmail import message_list_js

    answer = _run(
        message_list_js(),
        nodes={
            "tr.zA": {"attrs": {"class": "zA zE", "id": ":48"}},
            "[email]": {
                "attrs": {"email": "4cd598c01d62398ba33718c2199ba1c0@reply.craigslist.org"}
            },
            "[data-legacy-thread-id]": {"attrs": {"data-legacy-thread-id": "1a086d4fc4e321f6"}},
            ".bog": {"text": "is it available?"},
        },
    )
    assert answer["conversations"][0]["provider_thread_id"] == "1a086d4fc4e321f6"
    assert answer["conversations"][0]["unread"] is True
    # The widget id must not appear anywhere in the answer, under any field name.
    assert ":48" not in json.dumps(answer)


def test_the_mail_list_leaves_behind_a_row_it_cannot_identify() -> None:
    """A relay row with no stable id is counted, not folded in.

    Without an id nothing can tell the conversation apart on the next read, so returning it would
    re-journal it forever — the same failure as the widget id, arrived at by omission. Dropping it
    silently would instead hide a Gmail change, so the count is reported.
    """
    from sellee.mail.gmail import message_list_js

    answer = _run(
        message_list_js(),
        nodes={
            "tr.zA": {},
            "[email]": {
                "attrs": {"email": "4cd598c01d62398ba33718c2199ba1c0@reply.craigslist.org"}
            },
            ".bog": {"text": "is it available?"},
        },
    )
    assert answer["conversations"] == []
    assert answer["unidentified"] == 1


# --- an opened conversation, and the control a reply must never use ------------------------------


def test_the_conversation_tail_reads_stable_per_message_ids() -> None:
    """Captured live 2026-09-10 from a real buyer message.

    Note the two ids were the *same hex* on this single-message thread
    (`data-legacy-thread-id` and `data-legacy-message-id` both `1a086d4fc4e321f6`), so neither may
    be derived from the other — each is read from its own attribute.
    """
    from sellee.mail.gmail import conversation_tail_js

    answer = _run(
        conversation_tail_js(),
        nodes={
            ".adn": {"attrs": {"data-legacy-message-id": "1a086d4fc4e321f6"}},
            "[data-legacy-thread-id]": {"attrs": {"data-legacy-thread-id": "1a086d4fc4e321f6"}},
            "span[email]": {
                "attrs": {
                    "email": "4cd598c01d62398ba33718c2199ba1c0@reply.craigslist.org",
                    "name": "jerry neo",
                }
            },
            ".a3s": {"text": "is it available?\n\nOriginal craigslist post:\nhttps://x"},
        },
    )
    assert answer["opened_thread_id"] == "1a086d4fc4e321f6"
    assert answer["messages"][0]["provider_message_id"] == "1a086d4fc4e321f6"
    # Craigslist rewrites the address and not the display name — measured. The buyer's real name
    # arrives, and is kept: normal disclosure in both directions is how a craigslist sale works.
    assert answer["messages"][0]["sender_name"] == "jerry neo"


def test_the_conversation_tail_errors_rather_than_returning_an_empty_thread() -> None:
    """An empty tail is indistinguishable from "the buyer said nothing", and acting on that has the
    agent answer a conversation it cannot see."""
    from sellee.mail.gmail import conversation_tail_js

    answer = _run(conversation_tail_js())
    assert "error" in answer
    assert "messages" not in answer


def test_the_conversation_tail_never_carries_a_non_relay_body_out_of_the_page() -> None:
    """The list read's guard, applied again at the point where bodies — not just subjects — are
    read. A view can be changed under us between the two reads."""
    from sellee.mail.gmail import conversation_tail_js

    answer = _run(
        conversation_tail_js(),
        nodes={
            ".adn": {"attrs": {"data-legacy-message-id": "1a086d4fc4e321f6"}},
            "span[email]": {"attrs": {"email": "colleague@thecarousell.com"}},
            ".a3s": {"text": "Q3 board deck — confidential"},
        },
    )
    assert "error" in answer
    assert answer["blocked"] == 1
    assert "confidential" not in json.dumps(answer)


def test_the_reply_control_refuses_reply_all() -> None:
    """The trap the live capture exposed: Gmail puts "Reply", "Reply all" and "Forward" in one
    toolbar and matches on accessible name, so a substring test for "Reply" selects "Reply all".

    Reply-all on a relay thread copies every address craigslist put on the message — measured, both
    the `sale.` posting address and the `reply.` conversation address are on it — turning a private
    answer into a broadcast and mailing craigslist's own posting endpoint.
    """
    from sellee.mail.gmail import REPLY_CONTROL_JS

    answer = _run(REPLY_CONTROL_JS, nodes={"[aria-label]": {"attrs": {"aria-label": "Reply all"}}})
    assert "error" in answer
    assert answer["nearby"] == ["Reply all"]


def test_the_reply_control_finds_the_exact_reply_button() -> None:
    """`button[aria-label="Reply"]`, captured live 2026-09-10."""
    from sellee.mail.gmail import REPLY_CONTROL_JS

    answer = _run(REPLY_CONTROL_JS, nodes={"[aria-label]": {"attrs": {"aria-label": "Reply"}}})
    assert answer["label"] == "Reply"
    assert "error" not in answer


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


def test_the_mail_list_deduplicates_a_conversation_gmail_rendered_twice() -> None:
    """Gmail's row cache can hold two rows for one conversation — measured live 2026-09-10, after
    the tab had shown another view. Returned twice, the conversation is folded twice in one tick and
    the buyer is answered twice.
    """
    from sellee.mail.gmail import message_list_js

    answer = _run(
        message_list_js(),
        nodes={
            "tr.zA": {},
            "[email]": {
                "attrs": {"email": "4cd598c01d62398ba33718c2199ba1c0@reply.craigslist.org"}
            },
            "[data-legacy-thread-id]": {"attrs": {"data-legacy-thread-id": "1a086d4fc4e321f6"}},
            ".bog": {"text": "is it available?"},
        },
        rows={"tr.zA": 2},
    )
    assert len(answer["conversations"]) == 1
    assert answer["duplicates"] == 1


# --- the send, and the toast that must not be misread -------------------------------------------


def test_send_verify_never_reads_sending_as_sent() -> None:
    """The trap, captured live: at t+0.6s the toast said "Sending... Cancel". A verifier firing on
    the first `[role="alert"]` reports the buyer answered while the message is still in flight and
    still cancellable — the one wrong answer that cannot be walked back.
    """
    from sellee.mail.gmail import SEND_VERIFY_JS

    answer = _run(SEND_VERIFY_JS, nodes={'[role="alert"]': {"text": "Sending... Cancel"}})
    assert answer["sending"] is True
    assert "sent" not in answer


def test_send_verify_confirms_on_gmails_terminal_wording() -> None:
    """t+~1s, the settled state."""
    from sellee.mail.gmail import SEND_VERIFY_JS

    answer = _run(SEND_VERIFY_JS, nodes={'[role="alert"]': {"text": "Message sent"}})
    assert answer["sent"] is True


def test_send_verify_answers_unknown_once_the_toast_expires() -> None:
    """After about nine seconds the toast is gone (measured), and a send that worked is
    indistinguishable from one that never happened. That is `SendUnverified` — handed over,
    unconfirmable, never re-driven — and it must not be reported as either outcome.
    """
    from sellee.mail.gmail import SEND_VERIFY_JS

    answer = _run(SEND_VERIFY_JS)
    assert answer["unknown"] is True
    assert "sent" not in answer
    assert "sending" not in answer


def test_the_send_click_refuses_the_controls_that_merely_look_like_send() -> None:
    """ "Send feedback to Google" precedes the real control in DOM order and contains "Send", so the
    first substring match on the page opens a feedback dialog. Captured live 2026-09-10.
    """
    from sellee.mail.gmail import SEND_JS

    answer = _run(
        SEND_JS, nodes={"[aria-label]": {"attrs": {"aria-label": "Send feedback to Google"}}}
    )
    assert "error" in answer
    assert "clicked" not in answer


def test_the_compose_fill_refuses_when_the_body_will_not_take_the_text() -> None:
    """A contenteditable that silently ignored the write is otherwise indistinguishable from one
    that took it, and the difference is whether a buyer gets an empty message recorded as an answer.
    """
    from sellee.mail.gmail import compose_fill_js

    answer = _run(compose_fill_js("anything"), nodes={})
    assert "error" in answer
    assert "filled" not in answer


def test_compose_recipients_reads_chips_not_input_values() -> None:
    """Measured 2026-09-10: an inline reply's recipients are `[data-hovercard-id]` chips inside
    `[name="to"]`, and the field's `.value` is an empty string even when addressed. Reading the
    value would conclude the reply was addressed to nobody and send it anyway.
    """
    from sellee.mail.gmail import COMPOSE_RECIPIENTS_JS

    answer = _run(
        COMPOSE_RECIPIENTS_JS,
        nodes={
            '[name="to"]': {},
            "[data-hovercard-id]": {
                "attrs": {
                    "data-hovercard-id": "4cd598c01d62398ba33718c2199ba1c0@reply.craigslist.org"
                }
            },
        },
    )
    assert answer["to"] == ["4cd598c01d62398ba33718c2199ba1c0@reply.craigslist.org"]
    assert answer["cc"] == []


def test_the_handoff_scoped_artifacts_are_valid_javascript() -> None:
    """The factories build their JS by placeholder substitution rather than f-string escaping, and
    this is the guard for why: every one of these artifacts is full of literal `{`, an f-string
    needs each doubled, and a miscount produces a `SyntaxError` in the page — which surfaces as a
    mailbox reporting itself permanently unreadable. One such miscount happened while the handoff
    clause was being written.
    """
    from sellee.mail import gmail

    built = [
        gmail.message_list_js(),
        gmail.message_list_js("seller+cl@example.com"),
        gmail.conversation_tail_js(),
        gmail.conversation_tail_js("seller+cl@example.com"),
        gmail.REPLY_CONTROL_JS,
        gmail.COMPOSE_RECIPIENTS_JS,
        gmail.compose_fill_js("hello"),
        gmail.CLICK_REPLY_JS,
        gmail.open_conversation_js("1a08"),
        gmail.ACCOUNT_ADDRESS_JS,
        gmail.LOGIN_JS,
        gmail.SEND_JS,
        gmail.SEND_VERIFY_JS,
    ]
    for artifact in built:
        assert "{{" not in artifact, "a doubled brace escaped into the page"
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


def test_the_list_read_prefers_gmails_words_over_stale_rows() -> None:
    """Measured live 2026-09-10: a search for an unused subaddress rendered "No messages matched
    your search." *and* left 35 rows from the previous view in the DOM. Checking rows first — which
    this did — hands those leftovers back as live conversations of the current view, folding in
    stale threads and resurrecting a conversation that was already answered.
    """
    from sellee.mail.gmail import message_list_js

    answer = _run(
        message_list_js(),
        nodes={
            '[role="main"]': {"text": "No messages matched your search."},
            "tr.zA": {},
            "[email]": {"attrs": {"email": "4cd598@reply.craigslist.org"}},
            "[data-legacy-thread-id]": {"attrs": {"data-legacy-thread-id": "1a086d4fc4e321f6"}},
            ".bog": {"text": "a conversation from the previous view"},
        },
        rows={"tr.zA": 35},
    )
    assert answer["conversations"] == []
    assert answer["empty_stated"] is True
    # The residue is reported rather than hidden: a non-zero count is the signature of a reused tab.
    assert answer["stale_rows"] == 35


def test_a_forwarded_thread_is_admitted_by_its_content_not_its_sender() -> None:
    """The conversation the invitation creates, and the stage that decides it.

    A buyer who forwards writes from their own ordinary address, so the *list* read cannot vouch for
    them — it carries an opaque id and stops. The *tail* read has the body, and craigslist's footer
    plus a posting permalink is what admits it.
    """
    from sellee.mail.gmail import conversation_tail_js

    forwarded = (
        "still available?\n\nOriginal craigslist post:\nhttps://www.craigslist.org/view/d/x/abc123"
    )
    answer = _run(
        conversation_tail_js("seller@example.com"),
        nodes={
            ".adn": {"attrs": {"data-legacy-message-id": "m1"}},
            "[data-legacy-thread-id]": {"attrs": {"data-legacy-thread-id": "1a09ffff"}},
            "span[email]": {"attrs": {"email": "somebuyer@gmail.com", "name": "sam"}},
            ".a3s": {"text": forwarded},
        },
    )
    assert answer["messages"][0]["provider_message_id"] == "m1"
    assert answer["blocked"] == 0


def test_ordinary_mail_is_not_admitted_by_the_content_clause() -> None:
    """Both halves are required. The phrase alone admits a message that merely mentions craigslist;
    the URL alone admits a pasted link. Neither is a forwarded thread, and a body that satisfies
    neither does not leave the page at all."""
    from sellee.mail.gmail import conversation_tail_js

    for body in (
        "Q3 board deck — confidential",
        "I saw it on craigslist",
        "https://www.craigslist.org/view/d/x/abc123",
    ):
        answer = _run(
            conversation_tail_js("seller@example.com"),
            nodes={
                ".adn": {"attrs": {"data-legacy-message-id": "m1"}},
                "span[email]": {"attrs": {"email": "colleague@example.com"}},
                ".a3s": {"text": body},
            },
        )
        assert "error" in answer, body
        assert answer["blocked"] == 1
        assert "confidential" not in json.dumps(answer)


def test_the_sellers_own_reply_is_admitted_by_sender() -> None:
    """The scoped view includes the agent's own replies — they quote craigslist's footer — and that
    is what lets a later read settle a send whose confirmation was missed."""
    from sellee.mail.gmail import conversation_tail_js

    answer = _run(
        conversation_tail_js("seller@example.com"),
        nodes={
            ".adn": {"attrs": {"data-legacy-message-id": "m2"}},
            "span[email]": {"attrs": {"email": "seller@example.com"}},
            ".a3s": {"text": "Yes, still available."},
        },
    )
    assert answer["messages"][0]["provider_message_id"] == "m2"


# --- the mailbox login probe --------------------------------------------------------------------


def test_the_mailbox_probe_never_guesses_logged_out() -> None:
    """The same expensive wrong answer the market probe is shaped around: telling a signed-in
    seller to re-authenticate stops their Craigslist buyers being answered until they do.

    A page we do not recognise abstains, and so does the mail host with nothing rendered — which is
    what a still-loading Gmail looks like.
    """
    from sellee.mail.gmail import LOGIN_JS

    answer = _run(LOGIN_JS)
    assert answer["state"] == "unknown"


def test_the_mailbox_probe_reads_a_signed_in_mailbox() -> None:
    """Verified live 2026-09-10 against a real signed-in mailbox: host `mail.google.com` plus a
    positive marker of the rendered mail UI. `[role=main]` alone is too weak — the sign-in
    interstitial has one too."""
    from sellee.mail.gmail import LOGIN_JS

    answer = _run(LOGIN_JS, location={"hostname": "mail.google.com"}, nodes={'[gh="cm"]': {}})
    assert answer["state"] == "logged_in"
    assert answer["provider"] == "gmail"


def test_the_mail_host_with_nothing_rendered_abstains() -> None:
    """Still loading, or a shape we have not seen. Not a claim either way."""
    from sellee.mail.gmail import LOGIN_JS

    answer = _run(LOGIN_JS, location={"hostname": "mail.google.com"})
    assert answer["state"] == "unknown"
    assert answer["mail_ui"] is False


def test_the_mailbox_probe_reads_googles_own_sign_in_form() -> None:
    """Verified live in an incognito context: navigating the mailbox while signed out lands on
    `accounts.google.com` with an identifier field."""
    from sellee.mail.gmail import LOGIN_JS

    answer = _run(
        LOGIN_JS,
        location={"hostname": "accounts.google.com"},
        nodes={'input[type="password"]': {}},
    )
    assert answer["state"] == "logged_out"
    assert answer["provider"] == "gmail"


def test_a_password_box_off_googles_accounts_host_is_not_a_sign_out() -> None:
    """Both conditions together: a password box alone could be a re-auth panel inside a signed-in
    session, and reading it as signed out would stop a working mailbox."""
    from sellee.mail.gmail import LOGIN_JS

    answer = _run(
        LOGIN_JS,
        location={"hostname": "mail.google.com"},
        nodes={
            'input[type="password"]': {},
            '[gh="cm"]': {},
        },
    )
    assert answer["state"] == "logged_in"


def test_another_webmail_is_reported_as_an_unknown_provider() -> None:
    """Verified live: signing into Outlook in that window reports `provider: unknown`, so the
    connect refuses it by name instead of guessing at its DOM. A mailbox that reports itself
    permanently unreadable is worse than one that says "not this provider yet".
    """
    from sellee.mail.gmail import LOGIN_JS

    answer = _run(LOGIN_JS, location={"hostname": "outlook.live.com"})
    assert answer["provider"] == "unknown"
    assert answer["state"] == "unknown"


def test_a_lookalike_host_is_not_gmail() -> None:
    """Matched on a domain boundary: `mail.google.com.evil.com` is not the mail host."""
    from sellee.mail.gmail import LOGIN_JS

    answer = _run(
        LOGIN_JS, location={"hostname": "mail.google.com.evil.com"}, nodes={'[gh="cm"]': {}}
    )
    assert answer["provider"] == "unknown"
    assert answer["state"] == "unknown"
