"""The agent's Chrome window as a seam: where it is, whether it can be raised, and raising it.

Two callers want the same three answers and would otherwise each hold their own copy. `connect`
raises the window because the seller has to type into it; watch mode raises it because the seller
asked to see the work. Both need the same "which window, and where" wording, because both are read
on a phone and acted on at a desktop, and both must be honest about the raise simply not being
possible — off macOS, or with the browser on the seller's own machine rather than this one.

Every raise here is best-effort and never raises: `foreground` already answers False for every
reason it could not activate, and this adds the two policy gates in front of it. A raise that did
not land is a window the seller has to find, which the copy already covers; a raise that threw
would be a lane failing over a cosmetic.

Nothing here starts Chrome. With no Chrome on the port there is no pid to activate, so a raise
against a closed browser is a no-op — which is what makes it safe to call from a door that the
seller does not expect to open a window.
"""

from __future__ import annotations

import logging
import threading

from sellee import deployment, settings
from sellee.browser import chrome, foreground

log = logging.getLogger(__name__)

# Where the agent's Chrome window is. On a host install it is one this machine opened for itself
# and the seller has to tell it apart from their own; in a container it is theirs, on their own
# desktop, started by hand.
WINDOW_HERE = " — a separate window from your usual Chrome; check the Dock if you minimized it"
WINDOW_IN_CONTAINER = " on your own computer (the Chrome you started with start-chrome.sh)"

# The setting that says whether the seller is watching the work.
WATCH_SETTING = "watch_browser"


def where() -> str:
    """Where to look for the agent's window — a phrase that completes a sentence naming it."""
    return WINDOW_IN_CONTAINER if deployment.is_container() else WINDOW_HERE


def can_raise() -> bool:
    """Whether raising the agent's window is possible at all here.

    Two reasons it is not, and they are different: in a container the window belongs to a machine
    this process is not running on, and off macOS nothing in `foreground` can activate anything.
    Both mean the same thing to a caller — say where the window is instead of claiming it moved.
    """
    return not deployment.is_container() and foreground.is_supported()


def raise_now(cdp_port: int | None) -> bool:
    """Bring the agent's Chrome to the front, if that is possible. False for every reason it was
    not — never an exception, because no caller of this should fail over a window."""
    if not can_raise():
        return False
    try:
        return foreground.raise_window(chrome.resolve_port(cdp_port))
    except Exception:
        log.debug("could not raise the agent's Chrome window", exc_info=True)
        return False


def raise_if_watching(config, store) -> bool:
    """The same raise, gated on the seller having asked to watch. Called where the agent starts
    work worth seeing — a reply going out, a browser pass starting."""
    if not settings.get(store, WATCH_SETTING):
        return False
    return raise_now(getattr(config, "chrome_cdp_port", None))


def watch_raiser(config):
    """A bus subscriber: when watch mode goes on, bring the window forward once so the seller knows
    where to look.

    Subscribed rather than done at the door, because there are four doors — the card button,
    `/watch`, `sellee settings set`, and a proposal the seller approves — and one of them would
    otherwise have to be the one that also raises. Off-thread because the publisher may be the
    channel's receive loop, which is answering every other message in the chat: activation shells
    out, and a bounded few seconds there would stall the whole conversation.
    """

    def _on(event) -> None:
        if event.kind != "setting.changed":
            return
        payload = event.payload or {}
        if payload.get("key") != WATCH_SETTING or not payload.get("value"):
            return
        if not can_raise():
            return
        port = getattr(config, "chrome_cdp_port", None)
        threading.Thread(
            target=raise_now, args=(port,), name="watch-window-raise", daemon=True
        ).start()

    return _on
