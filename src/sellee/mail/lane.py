"""The lane that reads a mailbox, so a marketplace without an inbox still has buyers.

Craigslist buyers reach a seller by email. Everything above this line is reused untouched — the
threads store, `reconcile`, the reply lane and its coalesced pass, the intent lifecycle, the offline
scam pre-scan — so this lane's whole job is to turn a scoped mailbox read into the same rows the
browser inbox lane produces.

**It also finishes the connect.** A mailbox sign-in leaves two steps outstanding: confirming that
the scoped view is readable, and deriving the handoff address. Both need the browser, and the
connect lane has just spent its turn on the shared tab, so they are done here. Deliberately not
queued as a request row — `store.mail_ready` already says what is missing, so the work is derived
from state rather than from a queue that a restart could lose or duplicate.

Three things about this lane are not like the browser one, and each is a measured failure:

**A fresh tab per read.** Gmail keeps rendered list rows in the DOM across hash navigations, so a
tab that has shown `#inbox` still holds them inside a correctly-scoped search — 34 of the seller's
personal emails, measured. The scope guarantee is a property of the tab, not of the URL.

**Its own cadence, in hours.** The browser lane's 300s is for an inbox that changes minute to
minute. Mail does not, and a logged-in poll of a mailbox every five minutes is both pointless and
the strongest sustained signature this integration emits.

**Idempotence from a table, not a cursor.** A mailbox read has no cursor it can trust; a message
stays in the view and a provider may re-render it. Without `mail_relay_seen`, `reconcile.new_rows`
finds a new tail row every tick and the buyer appears to repeat themselves while the agent answers
each repeat.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from sellee import connectables, marketplaces, settings
from sellee.browser import inbox, reconcile
from sellee.browser import markets as market_adapters
from sellee.browser.client import BrowserDetached, BrowserError, BrowserUnavailable
from sellee.mail import connect as mail_connect
from sellee.mail import gmail, relay
from sellee.store.browser import CONNECT_MODE_OPEN
from sellee.store.helpers import StoreError

log = logging.getLogger(__name__)

# How often a mailbox is read. Hours, not minutes — see the module docstring.
MAIL_READ_INTERVAL_SEC = 900.0

# How many conversations one tick will open. A cap, because each is a navigation of the shared
# browser and a tick that opened fifty would hold the tab for minutes and starve the publish lane.
# Whatever is left over is read on the next tick; nothing is dropped.
MAX_CONVERSATIONS_PER_TICK = 5

# Said to the seller when a conversation cannot be attached to one of their items. Not silent: this
# is the answer to "why is nobody answering this buyer", and for a mail market the seller cannot
# see the agent's reasoning anywhere else.
UNPLACEABLE_NOTICE = (
    "Someone emailed about a {name} ad I couldn't match to one of your items — it's probably a "
    "listing you posted outside me. Their message is in your inbox and it's yours to answer."
)


@dataclass
class MailDeps:
    store: object
    bus: object
    config: object
    browser_factory: object
    now: Callable[[], float] = time.time
    # Per-market count of consecutive unreadable reads, so a mailbox that has gone quiet for a
    # reason we cannot see escalates instead of failing silently forever. In-process like the
    # browser lane's, and with the same limitation: a restart looks like a recovery.
    blind: dict = field(default_factory=dict)


def mail_lane(deps: MailDeps) -> None:
    """One tick: ask for any missing mailbox, finish an outstanding connect, then read."""
    connected = settings.connected_markets(deps.store)
    for market in connectables.MAIL_MARKETS:
        if market not in connected:
            continue
        transport = deps.store.mail_transport(market)
        if transport is None or not transport["signed_in"]:
            # No mailbox, on a market the seller has switched on. Ask for one — this is the only
            # route to a seller who was already using this market before the mail transport
            # existed, because `sellee update` swaps the version tree and re-runs no setup phase.
            # Without it they keep a connected marketplace that answers nobody, silently, forever.
            _ask_for_mailbox(deps, market)
            continue
        if inbox.browser_busy(deps.store):
            # A pass mid-drive owns the tab. A mailbox read is never urgent enough to pull a page
            # out from under a half-filled composer.
            return
        try:
            if not transport["view"] or not transport["verified_ts"]:
                _complete_connect(deps, market)
            else:
                _read(deps, market, transport)
        except BrowserDetached:
            # Ours, not theirs, and usually over within a tick — the factory replaces the server on
            # the next acquisition. Nothing is recorded, so the next tick simply tries again.
            return
        except (BrowserError, BrowserUnavailable) as exc:
            _count_blind(deps, market, str(exc))
            return


MAILBOX_NEEDED_NOTICE = (
    "One thing I can now do that I couldn't before: answer your {name} buyers. They email you "
    "rather than messaging in an app, so I need to read the mailbox that receives it — then I'll "
    "monitor it and reply for you. I only ever look at a search in it that shows your {name} mail, "
    "and you can see and change it whenever you like."
)


def _ask_for_mailbox(deps: MailDeps, market: str) -> None:
    """Ask once for the mailbox a switched-on market needs, and queue the sign-in.

    Once, keyed on there being no outstanding request: this lane ticks on its own cadence and a
    notice per tick would be a market nagging its seller. The row is what makes it once — the
    connect lane clears it when it has an answer, and `mail_ready` is what stops this asking again
    afterwards.
    """
    mail_target = connectables.mail_target_for(market)
    if not mail_target:
        return
    outstanding = {row["target"] for row in deps.store.pending_connects()}
    if mail_target in outstanding:
        return
    deps.store.request_connect(mail_target, CONNECT_MODE_OPEN)
    deps.store.queue_notice(
        MAILBOX_NEEDED_NOTICE.format(name=marketplaces.display_name(market)),
        controls=_signin_controls(market),
    )
    deps.bus.publish("mail.mailbox_needed", {"market": market})


# --- finishing the connect ----------------------------------------------------------------------


def _complete_connect(deps: MailDeps, market: str) -> None:
    """Confirm the scoped view is readable and derive the handoff address.

    Ordered so a half-finished connect is never recorded as a whole one: the view is written only
    after it has been *read*, and the handoff only after the view has been confirmed to query it
    coherently. A view written before it is read flips the seller from an actionable message ("sign
    in to your mailbox") to an unactionable one ("I can't read your mailbox").
    """
    name = marketplaces.display_name(market)
    client = deps.browser_factory()
    with client.exclusive():
        # The account's own address, which the handoff is derived from. Read in the same bracket as
        # everything else so a seller who signs out mid-connect cannot have one step answered about
        # one account and the next about another.
        with client.fresh_tab():
            client.navigate(gmail.SIGN_IN_URL)
            whose = client.evaluate(gmail.ACCOUNT_ADDRESS_JS) or {}
        address = str(whose.get("address") or "")
        handoff_address = mail_connect.derive_handoff(address)
        if not handoff_address:
            deps.store.queue_notice(
                mail_connect.VIEW_UNREADABLE_NOTICE.format(
                    name=name, reason="I couldn't read which account that mailbox belongs to"
                ),
                controls=_signin_controls(market),
            )
            return

        view = gmail.search_view(handoff_address)
        answer = mail_connect.read_scoped_view(client, view, handoff_address)

    if not mail_connect.view_is_readable(answer):
        reason = str(answer.get("error") or "the search didn't come back")
        deps.store.queue_notice(
            mail_connect.VIEW_UNREADABLE_NOTICE.format(name=name, reason=reason),
            controls=_signin_controls(market),
        )
        return

    # Both in one go, and only now: the read above is what makes either claim true.
    deps.store.record_mail_view(market, view)
    deps.store.record_mail_handoff(market, handoff_address, verified=True)
    deps.bus.publish("mail.connected", {"market": market, "handoff": handoff_address})
    deps.store.queue_notice(
        mail_connect.MAIL_READY_NOTICE.format(name=name, address=handoff_address)
    )


def _signin_controls(market: str):
    from sellee.channel import fastpaths

    mail_target = connectables.mail_target_for(market)
    return fastpaths.signin_controls(mail_target or market)


# --- reading -------------------------------------------------------------------------------------


def _read(deps: MailDeps, market: str, transport: dict) -> None:
    """Read the scoped view and fold anything new into threads."""
    handoff_address = transport["handoff_address"]
    client = deps.browser_factory()
    with client.exclusive():
        with client.fresh_tab():
            client.navigate(transport["view"])
            listed = client.evaluate(gmail.message_list_js(handoff_address)) or {}
            if "error" in listed:
                _count_blind(deps, market, str(listed.get("error")))
                return
            _clear_blind(deps, market)
            if listed.get("blocked"):
                # Not a filter statistic. A non-zero count means this tab is not the fresh one the
                # scope guarantee needs, which should be impossible here — reported so an
                # impossible thing is visible rather than quietly relied upon.
                log.warning("mail read saw %s out-of-scope rows in a fresh tab", listed["blocked"])
            conversations = listed.get("conversations") or []
            for found in conversations[:MAX_CONVERSATIONS_PER_TICK]:
                _read_conversation(deps, market, found, handoff_address, client)

    if len(conversations) > MAX_CONVERSATIONS_PER_TICK:
        log.info(
            "mail read capped at %s of %s conversations; the rest wait for the next tick",
            MAX_CONVERSATIONS_PER_TICK,
            len(conversations),
        )


def _read_conversation(deps: MailDeps, market: str, found: dict, handoff: str, client) -> None:
    """Open one conversation and fold its unseen messages into a thread."""
    provider_thread_id = found["provider_thread_id"]
    opened = client.evaluate(gmail.open_conversation_js(provider_thread_id)) or {}
    if "error" in opened:
        log.debug("could not open mail conversation %s: %s", provider_thread_id, opened["error"])
        return
    tail = client.evaluate(gmail.conversation_tail_js(handoff)) or {}
    if "error" in tail:
        log.debug("could not read mail conversation %s: %s", provider_thread_id, tail["error"])
        return
    if tail.get("opened_thread_id") and tail["opened_thread_id"] != provider_thread_id:
        # The click landed somewhere else. Refused rather than folded: attributing a buyer's
        # message to the wrong thread is the worst outcome available on this path.
        log.warning(
            "mail read asked for %s and got %s — refusing to fold",
            provider_thread_id,
            tail["opened_thread_id"],
        )
        return

    messages = tail.get("messages") or []
    unseen = [m for m in messages if m["provider_message_id"] not in _seen(deps, messages)]
    if not unseen:
        return

    # Craigslist puts the posting's permalink in every relay message, and unlike the address it
    # does not rotate — so it is the only join back to an item.
    posting_url = ""
    for message in messages:
        posting_url = relay.posting_url(message.get("body")) or posting_url
        if posting_url:
            break

    # Has a *buyer* written outside the relay? That is what moves the conversation to the direct
    # leg, and it is what decides whether a payment link may ever be sent.
    #
    # "Not a relay sender" is not enough, and getting this wrong would have been expensive: the
    # scoped view now includes the agent's own replies (they quote craigslist's footer), and those
    # come from the seller's own address. Counted naively, our first reply would move every
    # conversation to the direct leg by itself — and a payment link would go into the relay, where
    # it vanishes with no bounce. So the seller's own address is excluded too, leaving only a third
    # party writing direct.
    off_relay = any(_is_a_buyer_writing_direct(m["sender"], handoff) for m in messages)

    thread_id = relay.thread_key(provider_thread_id)
    if not _ensure_thread(deps, market, thread_id, posting_url, messages):
        return

    deps.store.upsert_mail_thread(
        provider_thread_id=provider_thread_id,
        thread_id=thread_id,
        market=market,
        relay_address=_reply_address(messages, handoff),
        posting_url=posting_url,
        off_relay=off_relay,
    )
    _fold(deps, market, thread_id, provider_thread_id, unseen)


def _seen(deps: MailDeps, messages) -> set:
    return deps.store.mail_messages_seen([m["provider_message_id"] for m in messages])


def _is_a_buyer_writing_direct(sender: object, handoff: str) -> bool:
    """Whether this message is a buyer writing outside the relay.

    Excludes craigslist's relay (that is the relay leg) and the seller's own address (that is us,
    or the seller writing by hand). What is left is a third party who has the seller's address —
    which is only true once the invitation has been acted on.
    """
    said = str(sender or "").strip().lower()
    if not said or relay.is_relay_sender(said):
        return False
    return said != str(handoff or "").strip().lower()


def _reply_address(messages, handoff: str = "") -> str:
    """Where the next reply goes: the address the newest *buyer* message arrived from.

    The newest and not the first, because craigslist mints a fresh relay address per view of a
    posting — five were observed for one posting, two from consecutive reloads seconds apart — so
    only the most recent one has any chance of still working.

    A buyer's, not any sender's: the view includes the agent's own replies, and recording the
    seller's own address here would aim the next reply at the seller.
    """
    for message in reversed(messages):
        if relay.is_relay_sender(message["sender"]):
            return message["sender"]
    for message in reversed(messages):
        if _is_a_buyer_writing_direct(message["sender"], handoff):
            return message["sender"]
    return ""


def _ensure_thread(deps: MailDeps, market: str, thread_id: str, posting_url: str, messages) -> bool:
    """Make sure a thread exists for this conversation, or say why it cannot.

    `store.create_thread` refuses a sell thread with no `item_id`, so a conversation that cannot be
    attached to one of our items cannot become a thread at all — the buyer is unanswerable by
    construction. That refusal is told to the seller rather than logged, because for a mail market
    there is nowhere else they could see it.
    """
    if deps.store.get_thread(thread_id) is not None:
        return True

    adapter = market_adapters.get_adapter(market)
    pattern = adapter.listing_id_pattern if adapter else ""
    product_id = reconcile.listing_id(posting_url, pattern) if posting_url and pattern else None
    items = deps.store.list_items()
    matches = reconcile.matching_items(product_id, items, market, pattern)
    if len(matches) != 1:
        deps.bus.publish(
            "mail.unplaceable",
            {
                "market": market,
                "thread_id": thread_id,
                "why": "unknown_listing" if not matches else "two_items",
            },
        )
        if not matches:
            deps.store.queue_notice(
                UNPLACEABLE_NOTICE.format(name=marketplaces.display_name(market))
            )
        return False

    # The buyer's own display name, which craigslist passes through unchanged (measured) — so a
    # reply can address a person. Falls back to the thread id rather than to a relay address, which
    # would be a handle that changes under them.
    handle = ""
    for message in messages:
        if not relay.is_relay_sender(message["sender"]):
            continue
        if message.get("sender_name"):
            handle = message["sender_name"]
            break
    try:
        deps.store.create_thread(
            thread_id=thread_id,
            side="sell",
            market=market,
            counterpart_handle=handle or thread_id,
            item_id=matches[0],
            source="mail_read",
        )
    except StoreError as exc:
        log.warning("could not create mail thread %s: %s", thread_id, exc)
        return False
    deps.bus.publish(
        "mail.thread_new", {"market": market, "thread_id": thread_id, "item_id": matches[0]}
    )
    return True


BUYER_WROTE_NOTICE = (
    "📩 A {name} buyer wrote about {item}:\n\n{said}\n\nI can read their message but {name} "
    "won't carry my reply back to them — so this one's yours to answer, in the mailbox it arrived "
    "in."
)


def _tell_the_seller(deps: MailDeps, market: str, thread_id: str, item_title: str, said: str):
    """Pass a buyer's message on, because we cannot answer it ourselves.

    This is what the mail transport still delivers for a market whose relay carries messages one
    way only (see `market_adapters.READ_ONLY_BUYERS`): the seller hears about a buyer in chat, with
    what they said, instead of finding out by watching an inbox. It is less than answering them and
    it is not nothing.

    The buyer's own words are included because the value is in not having to go and look, and the
    notice says plainly why the reply is theirs — a seller told only "someone wrote" would go
    hunting for a reply we never sent.
    """
    if market_adapters.answers_buyers(market):
        return
    deps.store.queue_notice(
        BUYER_WROTE_NOTICE.format(
            name=marketplaces.display_name(market),
            item=item_title or "one of your listings",
            said=said.strip()[:400],
        ),
        ref=f"mail-buyer:{thread_id}",
    )


def _fold(deps: MailDeps, market: str, thread_id: str, provider_thread_id: str, unseen) -> None:
    """Write the buyer's new messages, and record that they have been written.

    The scam pre-scan runs here for the same reason it runs in the browser lane: `tools/reply.py`
    gates a send on `scam_verdict` being stamped on the row, so a path that skips it does not fail
    loudly — it silently opens the gate.
    """
    stored = deps.store.get_thread_messages(thread_id, limit=None)
    folded = []
    for message in unseen:
        text = relay.buyer_text(message.get("body"))
        if not text:
            # Craigslist's footer and nothing else. Marked seen so it is not reconsidered every
            # tick, but never written: inventing a turn the buyer did not take would have the
            # agent answer an empty message.
            folded.append(message["provider_message_id"])
            continue
        verdict = inbox._scan(deps, {"thread_id": thread_id}, text, stored)["verdict"]
        deps.store.record_inbound(
            thread_id,
            msg_id=reconcile.message_id("in", text, 0),
            text=text,
            ts=deps.now(),
            direction="in",
            scam_verdict=verdict,
        )
        folded.append(message["provider_message_id"])
        deps.bus.publish(
            "mail.inbound",
            {"market": market, "thread_id": thread_id, "scam_verdict": verdict},
        )
        # Told once per message, and only for a market we cannot answer. Where we can, the reply
        # lane speaks and a notice here would be the seller hearing about it twice.
        thread = deps.store.get_thread(thread_id)
        item = deps.store.get_item(thread["item_id"]) if thread and thread.get("item_id") else None
        _tell_the_seller(deps, market, thread_id, (item or {}).get("title", ""), text)
    deps.store.mark_mail_messages_seen(provider_thread_id, folded)


# --- blindness -----------------------------------------------------------------------------------


# How many failed reads in a row before the seller hears about it. Not one: a single unreadable
# read is ordinary — a slow render, a tab that lost focus — and a notice per failure would be the
# transport crying wolf. Not ten either: every tick that passes is a buyer waiting.
BLIND_AFTER = 3

BLIND_NOTICE = (
    "I've stopped being able to read the mailbox your {name} buyers email — {reason}. Their "
    "messages are still arriving there; I just can't see them, so they're yours to answer until "
    "I can. Nothing else about {name} has changed."
)
READING_AGAIN_NOTICE = (
    "I can read the mailbox your {name} buyers email again. I've picked up anything that arrived "
    "while I couldn't."
)


def _count_blind(deps: MailDeps, market: str, reason: str) -> None:
    """Count a failed read, and say so once a run of them means we are genuinely blind.

    Said out loud because silence is indistinguishable from "no buyers wrote", and on a market
    whose ads are live that is the difference between a quiet week and a broken transport. Once
    per run, not once per tick — the counter is what makes it once.
    """
    failures = deps.blind.get(market, 0) + 1
    deps.blind[market] = failures
    log.warning("mail read failed for %s (%s in a row): %s", market, failures, reason)
    deps.bus.publish("mail.blind", {"market": market, "streak": failures, "reason": reason})
    if failures != BLIND_AFTER:
        # Exactly at the threshold: `>=` would re-notify on every tick after it.
        return
    deps.store.queue_notice(
        BLIND_NOTICE.format(name=marketplaces.display_name(market), reason=reason),
        controls=_signin_controls(market),
    )


def _clear_blind(deps: MailDeps, market: str) -> None:
    """A read worked again. Tell the seller only if they were told it had stopped."""
    failures = deps.blind.pop(market, 0)
    if not failures:
        return
    deps.bus.publish("mail.reading_again", {"market": market, "after": failures})
    if failures >= BLIND_AFTER:
        deps.store.queue_notice(READING_AGAIN_NOTICE.format(name=marketplaces.display_name(market)))
