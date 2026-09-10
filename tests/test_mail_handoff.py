"""Getting a buyer off craigslist's relay before it expires under them.

Craigslist mints a fresh relay address per view — five observed for one posting, two from
consecutive reloads seconds apart — and an expired one is unrecoverable: re-reading the posting
yields a `sale.` address, which is what a buyer writes to in order to reach the seller. So a relay
conversation has a shelf life, and the reply invites the buyer onto an address of the seller's own.

An earlier version of this file required a `+tag` on the address, so the read scope could be one
address existing only for craigslist. It was reverted, and the reason is the lesson worth keeping:
**an address that does not deliver is worse than a wide one.** A seller whose provider ignores
subaddressing gets an invitation naming an address nobody receives — a buyer lost silently, and
nobody knows they were lost. Reported by the seller whose address it was.

Scoping moved to content instead: a forwarded craigslist thread carries craigslist's own footer, and
that phrase is what the view searches for. Measured on a live mailbox, it matched four messages —
two buyer messages and the two replies to them — and nothing else.
"""

from __future__ import annotations

import pytest

from sellee.mail import gmail, handoff, relay

GOOD = "seller@example.com"
BUYER = "somebuyer@gmail.com"


# --- the address itself --------------------------------------------------------------------------


def test_an_ordinary_address_is_accepted_and_case_folded() -> None:
    assert handoff.parse("Seller@Example.com") == "seller@example.com"
    assert handoff.parse("  seller@example.com  ") == GOOD


def test_a_tagged_address_is_still_allowed() -> None:
    """Allowed, not required: a seller who has a working +tag may use it."""
    assert handoff.parse("seller+cl@example.com") == "seller+cl@example.com"


def test_no_address_configured_is_not_an_error() -> None:
    """A missing handoff address is a configuration gap, not a reason to stop answering buyers."""
    assert handoff.parse("") == ""
    assert handoff.parse(None) == ""


def test_a_plain_address_is_accepted_because_a_tag_may_not_deliver() -> None:
    """The reverted refusal, pinned so it does not come back. Requiring a `+tag` hands an
    undeliverable address to any seller whose provider ignores subaddressing."""
    assert handoff.parse("seller@example.com") == "seller@example.com"


def test_something_that_is_not_an_address_is_refused() -> None:
    for said in ("not an address", "two@at@signs.com", "seller name@example.com"):
        with pytest.raises(handoff.HandoffError):
            handoff.parse(said)


# --- the invitation -----------------------------------------------------------------------------


def test_the_line_names_the_address_and_asks_for_a_forward() -> None:
    """Forward, not reply: a forward carries craigslist's original message, and that message holds
    the posting permalink — the only non-rotating thing in a relay conversation and the join back
    to the item."""
    line = handoff.handoff_line(GOOD)
    assert GOOD in line
    assert "forward" in line.lower()


def test_the_line_carries_no_link() -> None:
    """It has to survive the one delivery that cannot be retried, and link-bearing relay mail is
    commonly dropped."""
    assert "http" not in handoff.handoff_line(GOOD)


def test_the_line_is_empty_when_no_address_is_configured() -> None:
    assert handoff.handoff_line("") == ""
    assert handoff.with_handoff("Yes, still available.", "") == "Yes, still available."


def test_the_invitation_is_appended_to_the_reply() -> None:
    body = handoff.with_handoff("Yes, still available.", GOOD)
    assert body.startswith("Yes, still available.")
    assert GOOD in body


def test_the_invitation_goes_out_once_per_conversation() -> None:
    """Repeated every message it reads as a bot and buries the answer the buyer asked for."""
    first = handoff.with_handoff("Yes, still available.", GOOD, already_sent=False)
    later = handoff.with_handoff("6pm works.", GOOD, already_sent=True)
    assert GOOD in first
    assert GOOD not in later
    assert later == "6pm works."


def test_the_invitation_passes_the_outbound_check() -> None:
    """It contains an email address, which is exactly what the reversed masking lint used to refuse.
    If that lint ever returns, this reply stops going out and the expiry problem comes back."""
    from sellee.mail import outbound

    body = handoff.with_handoff("Yes, still available.", GOOD)
    assert outbound.check(body, checkout_hosts=("carousell.ai",)) is None


# --- and the buyer who forwards must be readable -------------------------------------------------


def test_a_forwarded_thread_is_in_scope_by_its_content() -> None:
    """The whole point, and what would otherwise make the invitation self-defeating: a buyer who
    forwards writes from their own ordinary address, so no sender test can admit them. The forward
    carries craigslist's footer and the posting permalink, and that is checkable."""
    forwarded = (
        "still available?\n\nOriginal craigslist post:\nhttps://www.craigslist.org/view/d/x/abc"
    )
    assert relay.quotes_a_posting(forwarded) is True
    # By sender, a buyer writing direct is not in scope — which is why the content clause exists.
    assert relay.is_in_scope(BUYER, handoff=GOOD) is False


def test_the_rest_of_the_sellers_mailbox_stays_out_of_scope() -> None:
    """Matched on the whole address, never the domain: a domain match would admit everything the
    seller receives at work."""
    assert relay.is_in_scope("colleague@example.com", handoff=GOOD) is False
    assert relay.is_in_scope("seller+payroll@example.com", handoff=GOOD) is False


def test_ordinary_mail_is_not_admitted_by_the_content_clause() -> None:
    """Both halves required: the phrase alone admits a message that merely mentions craigslist, and
    the URL alone admits a pasted link."""
    assert relay.quotes_a_posting("I saw it on craigslist") is False
    assert relay.quotes_a_posting("https://www.craigslist.org/view/d/x/abc") is False
    assert relay.quotes_a_posting("Q3 board deck") is False


def test_a_lookalike_handoff_address_is_out_of_scope() -> None:
    assert relay.is_in_scope("seller@example.com.evil.com", handoff=GOOD) is False


def test_nothing_extra_is_in_scope_without_a_handoff_address() -> None:
    """With none configured the guard is exactly the relay guard it was before."""
    assert relay.is_in_scope(GOOD) is False
    assert relay.is_in_scope("4cd598@reply.craigslist.org") is True


def test_is_relay_sender_still_means_only_the_relay() -> None:
    """`is_in_scope` sits beside `is_relay_sender` rather than replacing it, because a lot depends
    on that function answering exactly one question."""
    assert relay.is_relay_sender(GOOD) is False
    assert relay.is_relay_sender("4cd598@reply.craigslist.org") is True


# --- the view, and the guard inside the page -----------------------------------------------------


def test_the_scoped_search_adds_the_footer_phrase() -> None:
    """The content clause: a forwarded craigslist thread carries craigslist's own footer, and only
    mail quoting a craigslist posting matches it. Measured live — four messages, nothing else."""
    view = gmail.search_view(GOOD)
    assert "sale.craigslist.org" in view
    assert "Original%20craigslist%20post" in view


def test_the_scope_does_not_depend_on_the_handoff_address() -> None:
    """The reverted design's clause was `deliveredto:<address>`, which required the provider to
    route subaddressing. The content clause requires nothing of it."""
    assert gmail.search_view("") == gmail.search_view(GOOD)
    assert "deliveredto" not in gmail.search_view(GOOD)


def test_the_quoted_phrase_survives_url_encoding() -> None:
    """The lesson from the `+` bug, kept: in a Gmail hash query `+` is the space separator, so a
    clause with unencoded punctuation silently becomes a different query — and the symptom is an
    empty mailbox with a buyer's thread sitting in it."""
    view = gmail.search_view(GOOD)
    assert " " not in view
    assert view.count("%22") == 2, "the phrase must stay quoted"


def test_a_seller_configured_label_is_left_alone() -> None:
    """It is the seller's own view; the point of letting them set it is that they decide what we
    can see, so the handoff is only appended to the search we build ourselves."""
    theirs = "https://mail.google.com/mail/u/0/#label/craigslist"
    assert gmail.scoped_view(theirs, GOOD) == theirs


def test_the_in_page_guard_carries_both_clauses() -> None:
    """The guard runs inside the page as well as in Python, so it needs the address *and* the
    footer phrase baked in."""
    built = gmail.message_list_js(GOOD)
    assert GOOD in built
    assert "sale.craigslist.org" in built
    assert relay.FOOTER_MARKER in built
    assert gmail.message_list_js(GOOD) != gmail.message_list_js()
