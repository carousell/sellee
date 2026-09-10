"""The one boundary a reply to a craigslist buyer must not cross.

Most of this file used to assert the opposite. An earlier decision refused an outbound reply that
contained an email address, a phone number or the seller's name, reasoning from a real measurement
(craigslist's relay rewrites the address and passes the display name through, so the body reaches
the buyer verbatim). The measurement holds; the conclusion did not. Exchanging a first name, a phone
number and a meeting spot is how a craigslist sale is actually arranged, and refusing it refuses the
transaction.

So the tests below are in two halves, and the first half is the larger one on purpose: **normal
craigslist disclosure must be allowed.** A regression here does not leak anything — it silently
stops the agent answering "when and where", which is the only question that closes a sale.

What is still refused is a checkout link, for reasons that are about deliverability and trust rather
than secrecy. See `sellee.mail.outbound`.
"""

from __future__ import annotations

from sellee.mail import outbound

HOSTS = ("carousell.ai",)


def _check(text: str):
    return outbound.check(text, checkout_hosts=HOSTS)


# --- normal craigslist disclosure is allowed ----------------------------------------------------


def test_a_phone_number_is_allowed() -> None:
    """The single most normal thing in a craigslist reply. Craigslist's own posting form invites a
    phone number, and pickup is arranged by text far more often than by email."""
    assert _check("Text me on 415-555-0199 and I'll hold it for you.") is None


def test_a_phone_number_in_every_written_shape_is_allowed() -> None:
    for said in ("(415) 555 0199", "+1 415 555 0199", "4155550199", "415.555.0199"):
        assert _check(f"call {said}") is None, said


def test_an_email_address_is_allowed() -> None:
    """A seller who would rather move off the relay is entitled to say so."""
    assert _check("Easier to reach me at seller@example.com.") is None


def test_the_sellers_name_is_allowed() -> None:
    assert _check("Thanks for asking! — Jerry") is None


def test_a_meeting_place_and_time_is_allowed() -> None:
    """The reply that closes the sale. This is the case the old lint broke."""
    said = "Yes, still available. I'm Jerry — 16th & Mission, Saturday 2pm? Text 415-555-0199."
    assert _check(said) is None


def test_an_ordinary_reply_is_allowed() -> None:
    said = "Yes, it's still available. Would you like to arrange a time to take a look?"
    assert _check(said) is None


def test_a_price_is_allowed() -> None:
    assert _check("It's $1,500.00 firm, and I can do this weekend.") is None


def test_the_craigslist_permalink_is_allowed() -> None:
    """Craigslist puts the posting's own URL in every relay message, so it must be quotable."""
    assert _check("https://www.craigslist.org/view/d/x/abc is the post.") is None


def test_an_unrelated_link_is_allowed() -> None:
    """The refusal is scoped to the checkout hosts, not to links in general."""
    assert _check("There's a photo here: https://example.com/lamp.jpg") is None


def test_an_empty_body_is_not_a_refusal() -> None:
    assert _check("") is None
    assert outbound.check(None) is None


# --- the checkout link, which is still refused ---------------------------------------------------


def test_a_checkout_link_is_refused() -> None:
    """Craigslist's own advice tells buyers never to pay through a link a seller sends, and
    link-bearing relay mail is commonly dropped — so this transport sends a code to type instead.

    Enforced here rather than in a prompt because the checkout tool hands back a live URL, and "the
    skill says not to" is one model mistake away from a payment link in a craigslist email.
    """
    found = _check("You can pay here: https://carousell.ai/checkout/abc123")
    assert found is not None
    assert found.reason == outbound.REASON_CHECKOUT_LINK


def test_a_checkout_link_on_a_subdomain_is_refused() -> None:
    found = _check("https://pay.carousell.ai/checkout/abc123")
    assert found is not None
    assert found.reason == outbound.REASON_CHECKOUT_LINK


def test_a_lookalike_checkout_host_is_not_refused_as_one() -> None:
    """`carousell.ai.evil.com` is not `carousell.ai`. Matching on a domain boundary matters as much
    here as it does on the inbound sender check."""
    assert _check("https://carousell.ai.evil.com/checkout/abc") is None


def test_nothing_is_refused_when_no_checkout_hosts_are_known() -> None:
    """The rail's base URL is configuration. With none supplied the honest behaviour is to refuse
    nothing, rather than to guess at what a checkout link looks like."""
    assert outbound.check("https://carousell.ai/checkout/abc123") is None


def test_a_transport_that_can_carry_links_refuses_nothing() -> None:
    """`link_hostile` is a property of the transport, not of craigslist — passed in so the same
    check serves a channel that can carry a URL perfectly well."""
    said = "https://carousell.ai/checkout/abc123"
    assert outbound.check(said, link_hostile=False, checkout_hosts=HOSTS) is None
