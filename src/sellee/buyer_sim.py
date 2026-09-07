"""A buyer the seller can play themselves, for exercising the reply loop without a marketplace.

Selling normally needs a real buyer on a real marketplace: the inbox lane drives Chrome, scrapes a
conversation, and the reply sink types back into it. That is impossible to rehearse and awkward to
test against, so this module supplies the two ends of that loop and nothing in between. Everything
the agent does between them — the reply pass, the model, `negotiate_offer`, the floor gate, minting
a carousell.ai checkout link — is the real thing, unchanged and unaware it is being rehearsed.

Simulated conversations live on their own market, `simbuyer`, and that choice is what keeps them
apart from real ones. `marketplaces.browser_markets()` does not list it, so the inbox lane never
tries to read a simulated thread with Chrome; and the sink below refuses any thread that is not on
it, so a real buyer is never quietly left unanswered while the simulator is running.

Off unless SELLEE_BUYER_SIM is set. The routes that use it 404 when it is unset, so a daemon that
was never told to simulate has no simulator.
"""

from __future__ import annotations

import os
import time
import uuid

from sellee.browser.client import BrowserError

# The market simulated conversations live on. Not a real marketplace and deliberately not shaped
# like one: a thread id is `<market>:<local id>`, so every simulated thread reads as `simbuyer:…`
# wherever thread ids surface — the event tail, the store, an escalation to the seller.
SIM_MARKET = "simbuyer"

_ENV_FLAG = "SELLEE_BUYER_SIM"


def enabled() -> bool:
    """Whether this daemon was started with the buyer simulator switched on."""
    return os.environ.get(_ENV_FLAG, "").strip() not in ("", "0", "false", "no")


def is_sim_thread(thread_id: str) -> bool:
    return str(thread_id or "").startswith(f"{SIM_MARKET}:")


class SimReplySink:
    """Stands in for the marketplace send on simulated threads.

    Delivery is a no-op because there is nowhere to deliver to: the "marketplace" is a page the
    seller has open. Returning cleanly is the whole point — `send_reply` only reaches
    `commit_reply` when the sink returns, and that commit is what writes the agent's words as an
    `out` row and advances the thread cursor. The simulator then just reads that row.

    A thread on any other market raises. It would be easier to swallow it, and much worse: a real
    buyer would sit unanswered with no trace. Raising leaves the intent `pending`, which the stale
    sweep turns into an `unconfirmed_send` escalation the seller can actually see.
    """

    def __init__(self, bus=None):
        self._bus = bus

    def send(self, thread: dict, text: str, kind: str, intent_id: str) -> None:
        thread_id = (thread or {}).get("thread_id", "")
        if not is_sim_thread(thread_id):
            raise BrowserError(
                f"the buyer simulator is running, so real marketplace sends are disabled; "
                f"thread {thread_id!r} was not delivered"
            )
        if self._bus is not None:
            self._bus.publish(
                "sim.reply",
                {"thread_id": thread_id, "kind": kind, "intent_id": intent_id, "text": text},
            )


def thread_id_for(local_id: str) -> str:
    return f"{SIM_MARKET}:{local_id}"


def record_buyer_message(store, *, item_id: str, handle: str, text: str, local_id: str) -> dict:
    """Put one buyer message on a simulated thread, creating the thread on first use.

    This is the same pair of store calls the browser inbox makes when it reads a real conversation
    (`create_thread` once, then `record_inbound` per message), and deliberately no more: no cursor
    is advanced here, because only a committed reply may do that. Write the message and the thread
    becomes eligible for a reply pass on its own.
    """
    thread_id = thread_id_for(local_id)
    # get_thread returns None for a thread that does not exist rather than raising, so this is a
    # None check and not a try/except.
    if store.get_thread(thread_id) is None:
        store.create_thread(
            thread_id=thread_id,
            side="sell",
            market=SIM_MARKET,
            counterpart_handle=handle,
            item_id=item_id,
        )
    # A real inbox reuses the marketplace's own message id, which is what makes record_inbound
    # idempotent. There is no marketplace here, and two identical messages a moment apart are a
    # thing a person testing a negotiation will absolutely do — so mint a fresh id rather than
    # deriving one from the clock, which would silently dedup the second.
    msg_id = f"in|{local_id}|{uuid.uuid4().hex[:12]}"
    store.record_inbound(thread_id, msg_id=msg_id, text=text, ts=time.time())
    return {"thread_id": thread_id, "msg_id": msg_id}
