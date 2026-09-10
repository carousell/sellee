"""The mail send: answering a buyer who reached the seller by email.

Answering a craigslist buyer means sending mail from the seller's own mailbox. That mailbox is
reached the way every other marketplace is — the seller signs into their webmail in the agent's own
Chrome, and the transport drives a **scoped view** of it — so **no credential is granted to us at
all**: no OAuth token, no app password, no API scope, nothing in `secrets.py` to leak, and no new
network surface.

The send is a sequence over the artifacts in `mail/gmail.py`, and its *order* is the design:

    open the conversation -> click Reply (exact label; never "Reply all")
      -> read the recipients and refuse unless every one is in scope, with no Cc or Bcc
      -> compose, appending the handoff invitation if this conversation has not had it
      -> check the body against the transport's one boundary
      -> stamp the intent unverified            <-- BEFORE the click, not after
      -> click Send (exact label; never "Send feedback to Google")
      -> poll: "Sending..." means wait, "Message sent" means accepted, silence means unverified

Four of those steps exist because of something measured, and none is defensive programming:

**The recipient check.** Craigslist mints a fresh relay address per view of a posting, and a
`sale.` address is what a *buyer* writes to in order to reach the *seller* — so a reply sent to a
freshly-read one mails the seller their own answer while the buyer waits. Gmail can also address a
reply to a resolved contact or carry a Cc craigslist put on the original. None of it bounces.

**The unverified stamp before the click.** The confirmation toast expires in about nine seconds. A
crash between the click and the read must not look like a send that never happened.

**"Sending..." is not "sent".** The first `[role="alert"]` after a click says "Sending... Cancel".
Read as success it reports a buyer answered while the message is still cancellable.

**And `sent` is not `delivered`.** A reply confirmed by Gmail, present in Sent, to a retired relay
address produced **no bounce of any kind** and never arrived. Nothing downstream may treat an
accepted send as a message the buyer has.
"""

from __future__ import annotations

import logging
import time

from sellee import settings
from sellee.browser.sink import SendNotAttempted, SendUnverified
from sellee.connectables import MAIL_MARKETS
from sellee.mail import gmail, handoff, outbound, relay

log = logging.getLogger(__name__)

# How long to wait for the send toast to settle, and how often to look. The toast goes
# "Sending..." -> "Message sent" in about a second and vanishes after about nine, so this window
# comfortably covers the transition without outliving the evidence.
SEND_SETTLE_SEC = 12.0
SEND_POLL_SEC = 0.5

NOT_CONNECTED = (
    "I can't answer {name} buyers yet: they reach you by email, and I don't have a mailbox to read "
    "them in. Connect the mailbox that receives your {name} mail and I'll take it from there."
)
NOT_SIGNED_IN = (
    "I can't answer {name} buyers right now — I'm signed out of the mailbox that receives their "
    "email. Sign back in and I'll pick up where I left off."
)
NOT_READY = (
    "I can't answer {name} buyers yet — their mailbox is connected but I haven't finished checking "
    "I can read just their mail. I'll try again shortly."
)
THREAD_UNKNOWN = (
    "I don't have a record of which email conversation {thread} is, so I can't reply to it."
)
THREAD_CLOSED = (
    "That {name} conversation can't be replied to any more: {reason}. Their message is in your "
    "inbox if you want to try yourself."
)


class MailReplySink:
    """A `ReplySink` that answers a buyer in the mailbox their message arrived in.

    Every refusal is `SendNotAttempted`, which is the honest class whenever nothing has been handed
    to a mail server: the intent stays retryable and no `sent_unverified` is minted for a message
    that was never composed. `SendUnverified` is reserved for the one case that earns it — Send was
    clicked and the confirmation could not be read.
    """

    def __init__(self, *, store, bus=None, config=None, browser_factory=None):
        self._store = store
        self._bus = bus
        self._config = config
        self._browser_factory = browser_factory

    def send(self, thread: dict, text: str, kind: str, intent_id: str) -> None:
        market = str((thread or {}).get("market") or relay.MARKET)
        name = _display(market)
        transport = self._store.mail_transport(market)

        if transport is None or not transport["view"]:
            raise SendNotAttempted(NOT_CONNECTED.format(name=name))
        if not transport["signed_in"]:
            # Distinguished from "not connected" because the seller can act on one of these and
            # not the other, and the earlier version of this sink could not tell them apart: it
            # branched on the view alone, so a signed-out seller was told to set up a mailbox they
            # had already set up.
            raise SendNotAttempted(NOT_SIGNED_IN.format(name=name))
        if not self._store.mail_ready(market):
            raise SendNotAttempted(NOT_READY.format(name=name))

        thread_id = str((thread or {}).get("thread_id") or "")
        conversation = _conversation_for(self._store, thread_id)
        if conversation is None:
            raise SendNotAttempted(THREAD_UNKNOWN.format(thread=thread_id))
        if conversation["state"] != "open":
            raise SendNotAttempted(
                THREAD_CLOSED.format(name=name, reason=conversation["closed_reason"] or "it ended")
            )

        if self._browser_factory is None:
            raise SendNotAttempted(NOT_CONNECTED.format(name=name))

        body = self._compose(conversation, text, transport)
        self._drive(transport, conversation, body, intent_id, name)

    # --- composing --------------------------------------------------------------------------

    def _compose(self, conversation: dict, text: str, transport: dict) -> str:
        """The body to send: the reply, plus the handoff invitation the first time only.

        The invitation is what saves a conversation from craigslist's per-view address rotation,
        and it is also the only route to a close — a payment link may only be sent once the buyer
        has moved onto the seller's own address (see `_link_hostile`). Once per conversation,
        because repeated every message it reads as a bot and buries the answer the buyer asked for.
        """
        body = handoff.with_handoff(
            text,
            transport["handoff_address"],
            already_sent=conversation["handoff_sent_ts"] is not None,
        )
        refusal = outbound.check(
            body,
            link_hostile=_link_hostile(conversation),
            checkout_hosts=_checkout_hosts(self._config),
        )
        if refusal is not None:
            raise SendNotAttempted(refusal.detail)
        return body

    # --- driving ----------------------------------------------------------------------------

    def _drive(
        self, transport: dict, conversation: dict, body: str, intent_id: str, name: str
    ) -> None:
        provider_thread_id = conversation["provider_thread_id"]
        client = self._browser_factory()
        with client.exclusive(), client.fresh_tab():
            client.navigate(transport["view"])
            opened = client.evaluate(gmail.open_conversation_js(provider_thread_id)) or {}
            if "error" in opened:
                raise SendNotAttempted(
                    f"I couldn't find that {name} conversation in your mailbox: {opened['error']}"
                )

            control = client.evaluate(gmail.REPLY_CONTROL_JS) or {}
            if "error" in control:
                raise SendNotAttempted(f"I couldn't open a reply: {control['error']}")
            click = client.evaluate(gmail.CLICK_REPLY_JS) or {}
            if "error" in click:
                raise SendNotAttempted(f"I couldn't open a reply: {click['error']}")

            self._check_recipients(client, transport, name)

            filled = client.evaluate(gmail.compose_fill_js(body)) or {}
            if "error" in filled:
                raise SendNotAttempted(f"I couldn't write the reply: {filled['error']}")

            # Before the click, never after. The toast lives about nine seconds, so a crash in
            # between must not read as a send that never happened.
            self._store.mark_intent_sent_unverified(intent_id)
            clicked = client.evaluate(gmail.SEND_JS) or {}
            if "error" in clicked:
                # Nothing was handed over: the control was not found, so no message left. The
                # unverified stamp above is the safe side of a race, and the reply lane settles it.
                raise SendNotAttempted(f"I couldn't send the reply: {clicked['error']}")
            verdict = self._settle(client)

        if verdict.get("sent"):
            self._store.mark_mail_handoff_sent(conversation["provider_thread_id"])
            self._publish("mail.sent", conversation, {"intent_id": intent_id})
            return
        # Handed over and unconfirmable. Never re-driven — the buyer may already have it.
        self._publish("mail.send_unverified", conversation, {"intent_id": intent_id})
        raise SendUnverified(
            f"I clicked send on that {name} reply but couldn't confirm it went — I won't send it "
            "again in case they already have it."
        )

    def _check_recipients(self, client, transport: dict, name: str) -> None:
        """Refuse unless every recipient is one this transport may write to, with no Cc or Bcc."""
        found = client.evaluate(gmail.COMPOSE_RECIPIENTS_JS) or {}
        if "error" in found:
            raise SendNotAttempted(
                f"I couldn't read who that reply is addressed to: {found['error']}"
            )
        everyone = list(found.get("to") or []) + list(found.get("cc") or [])
        everyone += list(found.get("bcc") or [])
        if not everyone:
            raise SendNotAttempted(f"that {name} reply came up addressed to nobody")
        stray = [
            address
            for address in everyone
            if not relay.is_in_scope(address, handoff=transport["handoff_address"])
        ]
        if stray:
            raise SendNotAttempted(
                f"that {name} reply came up addressed somewhere I don't recognise, so I didn't "
                "send it — it would not have reached the buyer"
            )
        if found.get("cc") or found.get("bcc"):
            raise SendNotAttempted(
                f"that {name} reply came up with other people copied in, so I didn't send it"
            )

    def _settle(self, client) -> dict:
        """Poll the confirmation until it settles, or until the evidence expires.

        "Sending..." means wait. Read as success it reports a buyer answered while the message is
        still cancellable — the one wrong answer here that cannot be walked back.
        """
        deadline = time.monotonic() + SEND_SETTLE_SEC
        verdict: dict = {}
        while time.monotonic() < deadline:
            verdict = client.evaluate(gmail.SEND_VERIFY_JS) or {}
            if verdict.get("sent") or verdict.get("unknown"):
                return verdict
            time.sleep(SEND_POLL_SEC)
        return verdict or {"unknown": True}

    def _publish(self, name: str, conversation: dict, extra: dict) -> None:
        if self._bus is None:
            return
        self._bus.publish(
            name,
            {
                "market": conversation["market"],
                "thread_id": conversation["thread_id"],
                "off_relay": conversation["off_relay_ts"] is not None,
                **extra,
            },
        )


def _link_hostile(conversation: dict) -> bool:
    """Whether this conversation's transport can carry a link — which is a per-*leg* question.

    On the **relay leg** a link is refused: link-bearing relay mail is commonly dropped, and
    craigslist's own advice tells buyers never to pay through a link a seller sends. On the
    **direct leg** — once the buyer has forwarded to the seller's own address — craigslist is not in
    the path at all, so the close is an ordinary checkout link like every other market.

    Read from durable state rather than inferred per send, because a restart that forgot would put
    a payment link into the relay, where it vanishes with no bounce and no error: the seller
    believes the buyer was asked to pay and the buyer never saw anything.
    """
    return conversation["off_relay_ts"] is None


def _checkout_hosts(config) -> tuple:
    """The hosts a checkout link lives on, from config. Empty refuses nothing, which is the honest
    behaviour when we do not know what a checkout link looks like."""
    base = str(getattr(config, "carousell_ai_web_base_url", "") or "")
    if "//" not in base:
        return ()
    host = base.split("//", 1)[1].split("/")[0].lower()
    return (host,) if host else ()


def _conversation_for(store, thread_id: str) -> dict | None:
    """The relay conversation behind a thread id, or `None` if there is no record of one."""
    if not thread_id:
        return None
    return store.mail_thread_by_thread_id(thread_id)


def _display(market: str) -> str:
    from sellee import marketplaces

    return marketplaces.display_name(market)


def mail_view(store) -> str:
    """The scoped webmail view this seller's craigslist mail is read through, or "" when unset.

    Kept for the seller's own override. The view the transport actually uses is the probed one on
    `mail_transport`, because that is the one something has read.
    """
    return str(settings.get(store, "craigslist_mail_view") or "")


def sink_factories(*, store, bus=None, config=None, browser_factory=None) -> dict:
    """Per-market send factories for every market answered by mail, for the composite sink.

    Returned as factories rather than sinks so nothing is built until a thread for that market
    actually needs sending — the same laziness the browser leaf needs, for the same reason.
    """

    def factory():
        return MailReplySink(store=store, bus=bus, config=config, browser_factory=browser_factory)

    return {market: factory for market in MAIL_MARKETS}
