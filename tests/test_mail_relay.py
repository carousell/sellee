"""The guard that keeps a mailbox integration to Craigslist's mail, and the thread key.

No mail provider offers a per-sender read scope — the narrowest read grant any of them has is the
whole mailbox — so "only read craigslist mail" is not something a credential can promise. It is
enforced here, and being enforced in our own code is exactly why it needs tests that try to get
past it.

The design puts a scoped *view* in front of this (a seller-created label, or a search) so the DOM
the transport reads only ever holds relay mail. This is the second layer: every row is checked
again before anything is parsed, so a view that is wrong or has been changed cannot leak a
message into the store.
"""

from __future__ import annotations

import pytest

from sellee.mail import relay

# --- the sender guard --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "rcc9la26d7534400a6a03514c34f9200@sale.craigslist.org",
        "rcc9la26d7534400a6a03514c34f9200@reply.craigslist.org",
        "RCC9LA26D7534400A6A03514C34F9200@SALE.CRAIGSLIST.ORG",
        "  rcc1@reply.craigslist.org  ",
        "Craigslist <rcc1@reply.craigslist.org>",
    ],
)
def test_a_relay_sender_is_recognised(address) -> None:
    assert relay.is_relay_sender(address)


@pytest.mark.parametrize(
    "address",
    [
        "",
        None,
        "someone@gmail.com",
        # The lookalikes. Each one is a domain an attacker can register.
        "rcc1@sale.craigslist.org.evil.com",
        "rcc1@notcraigslist.org",
        "rcc1@craigslist.org.co",
        "rcc1@xcraigslist.org",
        "rcc1@reply.craigslist.org.attacker.net",
        # Right domain, wrong subdomain: craigslist's own marketing and robot mail is not a buyer.
        "robot@craigslist.org",
        "no-reply@craigslist.org",
        # An address that merely mentions the relay domain in its local part.
        "reply.craigslist.org@evil.com",
    ],
)
def test_anything_else_is_refused(address) -> None:
    """A message that fails this is dropped unparsed, unlogged and unstored — the point is that
    nothing about the rest of the seller's mailbox reaches our code, not even a subject line."""
    assert not relay.is_relay_sender(address)


def test_the_check_is_on_the_domain_not_a_substring() -> None:
    """`"craigslist.org" in address` is the obvious implementation and it accepts every lookalike
    above. The boundary has to be a domain boundary."""
    assert not relay.is_relay_sender("rcc1@mail.craigslist.org.evil.com")
    assert relay.is_relay_sender("rcc1@sale.craigslist.org")


def test_only_relay_subdomains_count_not_the_bare_domain() -> None:
    """Craigslist sends plenty of mail from `craigslist.org` that is not a buyer — posting
    confirmations from `robot@`, for one. Folding those in as buyer messages would have the agent
    replying to craigslist's own robot."""
    assert not relay.is_relay_sender("anything@craigslist.org")


# --- pulling the addresses out of a message ----------------------------------------------------


def test_the_relay_address_is_taken_from_the_reply_target() -> None:
    """A reply has to go back through the relay, so the address we answer is the one craigslist
    put there for that purpose — never the display name and never a body signature."""
    found = relay.relay_addresses(
        {
            "from": "Craigslist <rcc-thread-1@reply.craigslist.org>",
            "reply_to": "rcc-thread-1@reply.craigslist.org",
            "to": "seller@example.com",
        }
    )
    assert found.reply_to == "rcc-thread-1@reply.craigslist.org"


def test_the_posting_address_is_kept_separately_from_the_conversation() -> None:
    """These are different facts. The `sale.` address identifies the *posting*, so it is how a
    message finds its item; the conversation address is what a reply goes to. Storing one in place
    of the other is how two buyers about one item become one thread.
    """
    found = relay.relay_addresses(
        {
            "from": "rcc-thread-1@reply.craigslist.org",
            "to": "rcc-posting-9@sale.craigslist.org",
        }
    )
    assert found.conversation == "rcc-thread-1@reply.craigslist.org"
    assert found.posting == "rcc-posting-9@sale.craigslist.org"


def test_a_message_with_no_relay_address_at_all_yields_nothing() -> None:
    found = relay.relay_addresses({"from": "someone@gmail.com", "to": "seller@example.com"})
    assert found.conversation is None
    assert found.posting is None


# --- the thread key: measured, not assumed ----------------------------------------------------

# Captured live 2026-09-10 from a real posting and a real buyer message.
POSTING_ADDRESS = "4cd598c01d62398ba33718c2199ba1c0@sale.craigslist.org"
INBOUND_SENDER = "4cd598c01d62398ba33718c2199ba1c0@reply.craigslist.org"


def test_a_relay_addresss_hex_identifies_nothing() -> None:
    """The finding that changed the design twice, pinned here so it is not re-derived wrongly.

    This module was planned on the assumption that `@reply.craigslist.org` was per-conversation, so
    a thread could be keyed on it. A first measurement — the posting's address and a buyer's sender
    sharing one hex — replaced that with "the hex belongs to the posting". **Both were wrong.**

    Five distinct addresses were then observed for one live posting (id 7963439877), two of them
    from consecutive reloads of the same page seconds apart. Craigslist mints a fresh address per
    *view*: it is not a posting id, not a conversation id, not a viewer id.

    `posting_hex` therefore raises rather than returning a value, so a caller reintroduced from the
    old design fails loudly instead of joining a buyer's message to an arbitrary item.
    """
    for address in (POSTING_ADDRESS, INBOUND_SENDER, "someone@gmail.com"):
        with pytest.raises(NotImplementedError) as caught:
            relay.posting_hex(address)
        assert "posting_url" in str(caught.value)


def test_a_relay_address_is_refused_as_a_thread_key() -> None:
    """Refused rather than merely discouraged: it is the one mistake here with no visible symptom
    until two buyers are reading each other's messages."""
    for address in (POSTING_ADDRESS, INBOUND_SENDER):
        with pytest.raises(ValueError) as caught:
            relay.thread_key(address)
        assert "posting" in str(caught.value)


def test_the_thread_key_is_the_providers_own_conversation_id() -> None:
    """The provider groups a conversation for its own reasons, and that grouping is the only
    per-conversation fact a scoped mailbox read has."""
    key = relay.thread_key("thread-f:1839920011223344")
    assert key == "craigslist:thread-f:1839920011223344"
    assert key.startswith("craigslist:")


def test_two_conversations_about_one_posting_are_two_threads() -> None:
    """The property the key exists for, now expressed against what actually distinguishes them."""
    assert relay.thread_key("thread-f:111") != relay.thread_key("thread-f:222")


def test_the_same_conversation_is_always_the_same_thread() -> None:
    assert relay.thread_key(" thread-f:111 ") == relay.thread_key("thread-f:111")


def test_a_conversation_with_no_provider_id_is_refused() -> None:
    with pytest.raises(ValueError):
        relay.thread_key("")
    with pytest.raises(ValueError):
        relay.thread_key(None)


def test_the_posting_is_identified_by_the_permalink_not_the_address() -> None:
    """The address identifies nothing, so the only join to an item is the permalink craigslist puts
    in the message body — the one thing about a relay conversation that does not rotate."""
    body = (
        "is it available?\n\nOriginal craigslist post:\nhttps://www.craigslist.org/view/d/x/abc123"
    )
    assert relay.posting_url(body) == "https://www.craigslist.org/view/d/x/abc123"
    assert relay.posting_url("no link here") is None


# --- the send path's refusals, which are the states a seller can act on ------------------------

# The sink's happy path is driven in `tests/test_mail_send.py`, against a stub browser. What is
# pinned here is that each refusal is a *different* situation, because an earlier version branched
# on the mail view alone and so told a signed-out seller to connect a mailbox they already had.


def _thread() -> dict:
    return {"thread_id": "craigslist:1a08", "market": "craigslist"}


def test_no_mailbox_refuses_with_a_reason_the_seller_can_act_on(store) -> None:
    """Why the seam matters at all. Without it a craigslist thread's reply falls through to the
    marketplace sink and fails as `no browser adapter for 'craigslist'` — true of nothing anyone
    can fix, and it points the reader at the browser layer, which is not where these buyers are.
    """
    from sellee.browser.sink import SendNotAttempted
    from sellee.mail import transport

    sink = transport.MailReplySink(store=store)
    with pytest.raises(SendNotAttempted) as caught:
        sink.send(_thread(), "hello", "reply", "int_1")
    said = str(caught.value)
    assert "Craigslist" in said
    assert "email" in said
    assert "browser adapter" not in said


def test_the_four_refusals_are_four_different_situations(store) -> None:
    """Each is fixed by a different action — by the seller, by waiting, or by nobody — so telling
    them apart is the whole value of the checks."""
    from sellee.browser.sink import SendNotAttempted
    from sellee.mail import transport

    sink = transport.MailReplySink(store=store)
    said = []

    # 1. nothing connected
    with pytest.raises(SendNotAttempted) as caught:
        sink.send(_thread(), "hi", "reply", "i1")
    said.append(str(caught.value))

    # 2. connected, signed out — theirs to fix, and the case the old sink could not see
    store.record_mail_probe("craigslist", provider="gmail", signed_in=False)
    store.record_mail_view("craigslist", "https://mail.google.com/#search/x")
    with pytest.raises(SendNotAttempted) as caught:
        sink.send(_thread(), "hi", "reply", "i2")
    said.append(str(caught.value))

    # 3. signed in, connect not finished — ours, and it resolves itself
    store.record_mail_probe("craigslist", provider="gmail", signed_in=True)
    with pytest.raises(SendNotAttempted) as caught:
        sink.send(_thread(), "hi", "reply", "i3")
    said.append(str(caught.value))

    # 4. ready, but no record of which email conversation this thread is
    store.record_mail_handoff("craigslist", "you+cl@example.com", verified=True)
    with pytest.raises(SendNotAttempted) as caught:
        sink.send(_thread(), "hi", "reply", "i4")
    said.append(str(caught.value))

    assert len(set(said)) == 4, said


def test_a_closed_conversation_is_refused_with_the_reason_it_ended(store) -> None:
    """Craigslist retires a relay address and the conversation becomes unreachable. Sending into it
    would be a message nobody receives, reported as an answer."""
    from sellee.browser.sink import SendNotAttempted
    from sellee.mail import transport

    store.record_mail_probe("craigslist", provider="gmail", signed_in=True)
    store.record_mail_view("craigslist", "https://mail.google.com/#search/x")
    store.record_mail_handoff("craigslist", "you+cl@example.com", verified=True)
    store.upsert_mail_thread(
        provider_thread_id="1a08", thread_id="craigslist:1a08", market="craigslist"
    )
    store.close_mail_thread("1a08", "unreachable", "craigslist retired the address")

    sink = transport.MailReplySink(store=store)
    with pytest.raises(SendNotAttempted) as caught:
        sink.send(_thread(), "hi", "reply", "i1")
    assert "retired the address" in str(caught.value)


def test_the_daemon_routes_craigslist_to_mail_and_everything_else_to_the_browser(store) -> None:
    """The wiring, asserted where it is easy to get wrong: a factory map that missed craigslist
    would route its buyers into the browser and fail confusingly, and one that caught too much
    would route Carousell's chat into a mailbox."""
    from sellee.mail import transport

    factories = transport.sink_factories(store=store)
    assert set(factories) == {"craigslist"}


# --- the address rotates, so nothing may be built on it holding still --------------------------

# The real bounce, verbatim from a second message sent to the address the posting showed earlier.
BOUNCE_550 = (
    "550 [5F610D08-C847-4E28-B2C7-3C75366E12CB.1@mxi7a] Please visit "
    "https://www.craigslist.org/view/d/san-francisco-dji-mic-mini-wireless/q2Dcuy4bHvytRxM1T27fC9 "
    "to get a current reply email address. For more info, see: "
    "https://www.craigslist.org/about/help/email_relay_error"
)


def test_a_stale_address_bounce_is_not_a_closed_conversation() -> None:
    """The distinction that decides whether a buyer keeps being answered.

    Craigslist rotates a posting's relay address: an address that worked earlier answers `550 …
    get a current reply email address` later. Read as the thread ending — a buyer opting out, or a
    four-month expiry — that buyer is silently abandoned mid-conversation. It means the opposite:
    the conversation is alive and the address needs re-reading.
    """
    assert relay.bounce_kind(BOUNCE_550) == "stale_address"


def test_an_opt_out_bounce_is_a_closed_conversation() -> None:
    """The other direction, and it must not be retried: a buyer who opted out is not reachable by
    re-reading anything."""
    said = "550 the recipient has opted out of receiving messages from this posting"
    assert relay.bounce_kind(said) == "opted_out"


def test_an_unrecognised_bounce_is_not_guessed() -> None:
    """Neither retried forever nor treated as an ending — reported, so it is looked at once."""
    assert relay.bounce_kind("451 temporary local problem") == "unknown"
    assert relay.bounce_kind("") == "unknown"


def test_the_posting_url_is_taken_from_the_message_not_the_address() -> None:
    """Which item a conversation is about has to come from something that does not rotate.

    The hex does: two buyers writing in different rotation windows carry different ones, so it
    cannot join a message to an item. Craigslist puts the posting's own URL in the message body,
    and that is stable — it is what a stored listing URL can actually be matched against.
    """
    body = (
        "someone is interested in your posting\n\n"
        "https://www.craigslist.org/view/d/san-francisco-dji-mic-mini-wireless/"
        "q2Dcuy4bHvytRxM1T27fC9\n\nis it available?"
    )
    assert relay.posting_url(body) == (
        "https://www.craigslist.org/view/d/san-francisco-dji-mic-mini-wireless/"
        "q2Dcuy4bHvytRxM1T27fC9"
    )


def test_a_message_with_no_posting_url_yields_nothing() -> None:
    assert relay.posting_url("just a message with no link") is None


def test_a_non_craigslist_link_in_a_body_is_not_taken_as_the_posting() -> None:
    """A buyer can paste anything, including a link designed to be mistaken for the posting."""
    assert relay.posting_url("see https://evil.example/view/d/thing/abc") is None
    assert relay.posting_url("https://www.craigslist.org.evil.com/view/d/x/abcdefghij") is None


# --- craigslist's footer, and why it must not become the buyer's words --------------------------

# One real relay message, captured verbatim from a live posting on 2026-09-10.
CAPTURED_BODY = (
    "is it available?\n\n\n\n\n"
    "Original craigslist post:\n"
    "https://www.craigslist.org/view/d/san-francisco-dji-mic-mini-wireless/q2Dcuy4bHvytRxM1T27fC9\n"
    "About craigslist mail:\n"
    "https://www.craigslist.org/about/help/posting/features/contact-info/email/mail-relay\n"
    "Please flag unwanted messages (spam, scam, other):\n"
    "https://post.craigslist.org/mailflag?flagCode=34&smtpid=e6ffc83cccf4bc773d874f9d843fbdb1"
)


def test_buyer_text_keeps_only_what_the_buyer_wrote() -> None:
    """Left in, craigslist's three-block footer is journaled as the buyer's turn: compared by
    `reconcile` when aligning a tail, and read by the model as though the buyer pasted three URLs
    and asked to flag themselves for spam."""
    assert relay.buyer_text(CAPTURED_BODY) == "is it available?"


def test_buyer_text_carries_no_craigslist_links_into_the_model() -> None:
    """The publish recipe is told to navigate only URLs it was handed. A buyer turn that smuggles
    craigslist's own links in is the shape that rule exists to prevent."""
    kept = relay.buyer_text(CAPTURED_BODY)
    assert "craigslist.org" not in kept
    assert "mailflag" not in kept


def test_buyer_text_cuts_at_the_earliest_marker_whatever_the_order() -> None:
    """The footer's blocks are matched by craigslist's fixed labels, not by position, so a
    reordered footer still goes entirely."""
    reordered = (
        "still for sale?\n\nAbout craigslist mail:\nhttps://x\nOriginal craigslist post:\nhttps://y"
    )
    assert relay.buyer_text(reordered) == "still for sale?"


def test_buyer_text_keeps_the_newest_words_when_the_buyer_quotes_a_footer() -> None:
    """A buyer replying to our reply quotes the older footer below their new text. Cutting at the
    first marker keeps exactly the new text, which is the right answer for a top-posting client."""
    threaded = (
        "yes 6pm works\n\n"
        "Original craigslist post:\nhttps://x\n"
        "On Tue, someone wrote:\n> is it available?\n"
    )
    assert relay.buyer_text(threaded) == "yes 6pm works"


def test_buyer_text_returns_empty_for_a_message_that_is_only_footer() -> None:
    """Honest: the caller decides what an empty buyer turn means. Returning the footer instead
    would invent words the buyer never wrote."""
    assert relay.buyer_text("Original craigslist post:\nhttps://x") == ""


def test_buyer_text_passes_through_a_body_with_no_footer() -> None:
    """Not every message carries one — a bounce does not, and neither does mail from a relay we
    have not seen. A missing footer is not a reason to lose the body."""
    assert relay.buyer_text("  is it still available?  ") == "is it still available?"
    assert relay.buyer_text("") == ""
    assert relay.buyer_text(None) == ""
