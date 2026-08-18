"""Carousell's browser contract: the conversation list, the message read, the composer, the login
probe.

The conversation list comes from Carousell's own JSON API, fetched from the page so the session
cookie rides along. The inbox DOM cannot supply it: its rows are `div[role="button"]` with hashed
classes and carry no link, id or data attribute, so a conversation there has no addressable identity
at all. The API answers with the identity plus the counterpart, the listing, and the unread count —
and it fails with a status code, where a DOM read that finds nothing looks exactly like an inbox
with nothing in it.

Message history is the one thing the API does not carry (chat lives in a separate service), so the
tail read is DOM work and is the layer's only remaining DOM knowledge. It is written
class-agnostically — Carousell ships hashed CSS classes that churn on every deploy — locating by
scroll container, by geometry, and by the shape of a message bubble.
"""

from __future__ import annotations

# Conversations with Carousell itself — the platform assistant and its promotional accounts — rather
# than with a buyer. Never conversations to answer. (`is_bot_offer` looks like it should say this
# and does not: it is false for the campaign accounts.)
SYSTEM_HANDLES = frozenset(
    {
        "carousell_assistant",
        "carousell_campaigns_sg",
        "carousell_promote",
        "carousell_sg",
        "selltocarousell_mobiles",
        "carousell",
    }
)

# Where a listing's id sits in its permalink (`/p/<slug>-<id>/`). The API names a conversation's
# listing by that same id, so this is what joins a conversation to one of our own items — an exact
# match, rather than hunting for a title inside a preview string.
LISTING_ID_PATTERN = r"/p/(?:[^/]*-)?(\d+)/?$"

# Carousell's own conversation list. A conversation is an *offer* in its model, and the offer id is
# the id in the chat URL, so this is also where a thread's durable key comes from.
#
# Fetched same-origin from whatever Carousell page is open: the session cookie is sent
# automatically, no page has to be visited, and — unlike opening a conversation — nothing is
# marked read.
#
# Returns `{conversations: [...]}` on a good read and `{error: …}` otherwise. The distinction is the
# point: an empty list here genuinely means the seller has no conversations, whereas a failure says
# so out loud instead of quietly looking like an empty inbox.
#
# The id comes from `legacy_offer_id`, not `id`. `id` is a 32-bit integer server-side and has
# wrapped, so a new conversation reports a negative one, which in the chat URL is not that
# conversation. `legacy_offer_id` is a string and carries the true value.
CONVERSATIONS_LIST_JS = """async () => {
  try {
    const res = await fetch(
      '/ds/offer/1.0/me/?_path=%2F1.0%2Fme%2F&count=50&l=en&type=all',
      { headers: { accept: 'application/json' }, credentials: 'same-origin' }
    );
    if (!res.ok) return { error: 'HTTP ' + res.status };
    const body = await res.json();
    const offers = ((body || {}).data || {}).offers;
    if (!Array.isArray(offers)) return { error: 'no offers array in the response' };
    return {
      conversations: offers.map((o) => {
        const user = o.user || {};
        const product = o.product || {};
        return {
          thread_id: String(o.legacy_offer_id || o.id || ''),
          handle: String(user.username || ''),
          product_id: product.id ? String(product.id) : null,
          title: String(product.title || ''),
          unread: Number(o.unread_count) || 0,
          last_message: String(o.latest_price_message || ''),
          offer_type: String(o.offer_type || ''),
        };
      }),
    };
  } catch (e) {
    return { error: String(e).slice(0, 200) };
  }
}"""

# Read the trailing message bubbles of the open conversation.
#
# Two filters do the work, and both are about *not* inventing messages nobody sent. The chat pane is
# full of text that reads like a short message — the counterpart's name and "Online 11 days ago" in
# the header, a profile card ("14 verified orders", "11 years on Carousell"), system notices
# ("Message blocked", "Older messages have been…"), per-message timestamps, and Carousell's
# quick-reply suggestion chips ("Yes?", "Interested?"). A chip in particular is indistinguishable
# from a real buyer message by text alone, and recording one would have the agent answering itself.
#
#   1. Scope to the message list: the single scrollable pane on the chat side of the page.
#   2. Keep only what is actually a bubble: a bubble is an *inline* rounded container, where every
#      one of the impostors above sits in a block/flex container with square corners. Clickable text
#      is dropped as well, which is a second, independent guard on the chips.
#
# Direction comes from GEOMETRY, never from CSS classes: within the list, an outbound bubble hugs
# the right edge and an inbound one hugs the left. Anything roughly centred is reported as "center"
# so the caller ignores it rather than mistaking it for something said.
#
# Returns null when the message list cannot be identified — the caller must treat that as a failed
# read, because an empty list would claim the conversation is over when we simply could not see it.
#
# The function is async because the chat messages are fetched after the page load event fires: a
# synchronous read on a freshly navigated tab finds the pane but no bubbles yet, which the caller
# cannot distinguish from a genuinely empty thread. Polling closes that gap without changing what
# null vs [] means to the caller.
CONVERSATION_TAIL_JS = """async () => {
  const cut = window.innerWidth * 0.35;
  const clickable = (el) => {
    for (let n = el, i = 0; n && i < 6; n = n.parentElement, i++) {
      if (getComputedStyle(n).cursor === 'pointer') return true;
    }
    return false;
  };
  const inBubble = (el) => {
    for (let n = el, i = 0; n && i < 3; n = n.parentElement, i++) {
      const st = getComputedStyle(n);
      if (st.display === 'inline-flex' && (parseFloat(st.borderRadius) || 0) > 0) return true;
    }
    return false;
  };
  const read = () => {
    const panes = Array.from(document.querySelectorAll('div')).filter((el) => {
      if (!/auto|scroll/.test(getComputedStyle(el).overflowY)) return false;
      const r = el.getBoundingClientRect();
      return r.width > 200 && r.height > 120 && r.left > cut;
    });
    if (panes.length !== 1) return null;
    const pane = panes[0];
    const pr = pane.getBoundingClientRect();
    const out = [];
    pane.querySelectorAll('p').forEach((el) => {
      if (el.children.length > 0) return;
      const r = el.getBoundingClientRect();
      if (r.width === 0) return;
      const text = (el.textContent || '').trim();
      if (!text || clickable(el) || !inBubble(el)) return;
      const fromLeft = r.left - pr.left;
      const fromRight = pr.right - r.right;
      let side = 'center';
      if (fromRight < fromLeft * 0.6) side = 'out';
      else if (fromLeft < fromRight * 0.6) side = 'in';
      out.push({ text: text.slice(0, 300), side: side, y: Math.round(r.top) });
    });
    out.sort((a, b) => a.y - b.y);
    return out;
  };
  const deadline = Date.now() + 5000;
  let result = read();
  while (result !== null && result.length === 0 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 250));
    result = read();
  }
  return result;
}"""

# Submit the composed message, by dispatching the key the box listens for.
#
# There is no send control to click — the icon sits inside undecorated elements with no role, no
# label and no cursor change — so the only ways to submit are a real key event or this. A real one
# reaches only the *active* tab, so it pulls the seller's window in front of whatever they were
# doing; dispatching from the page interrupts nothing but carries `isTrusted: false`. Carousell
# takes the second trade because the page's own handler is known: it tests the key, the trimmed
# length, a guard ref and Shift, and nothing else. The message text still arrives as real input;
# only the submit is synthesised.
#
# `preventDefault` is called only inside that handler's send branch, so its having been called is
# the page acknowledging the message. Reported back as `sent`, which is what lets a refusal stay
# retryable instead of becoming a message nobody can account for.
CHAT_MESSAGE_SUBMIT_JS = """(el) => {
  const ev = new KeyboardEvent('keydown', {
    key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
    bubbles: true, cancelable: true, composed: true,
  });
  el.dispatchEvent(ev);
  return { sent: ev.defaultPrevented };
}"""

# Is the seller logged in? Three-state, and it must never answer logged_out on thin evidence: a
# false logged_out tells a perfectly signed-in seller to re-authenticate and stops their market.
# Only an auth-gated control present (inbox / sell) proves logged_in; only a login control with no
# such marker proves logged_out; anything else is unknown.
LOGIN_JS = """() => {
  try {
    const inbox = !!document.querySelector('a[href="/inbox/"], a[href$="/inbox/"]');
    const sellQuery = 'a[href="/sell/"], a[href$="/sell/"], a[href*="/sell/new"]';
    const sell = !!document.querySelector(sellQuery);
    if (inbox || sell) return { state: 'logged_in' };
    const text = (document.body && document.body.innerText) || '';
    if (/\\bLog in\\b|\\bSign up\\b/i.test(text)) return { state: 'logged_out' };
    return { state: 'unknown' };
  } catch (e) {
    return { state: 'unknown' };
  }
}"""

# The reply composer, as shipped defaults under the heal cache. A chat page has exactly one message
# box, so this stays class-agnostic; when Carousell moves it, the resolve finds nothing, the send
# fails closed before anything is typed, and the healed selector is what the cache learns.
#
# There is no send-button entry because there is nothing addressable to click: the send icon's
# ancestors are undecorated elements with no role, no label and no cursor change, while the message
# box handles Enter itself. Enter is the send.
COMPOSER_DEFAULTS = (
    {
        "step": "message_box",
        "strategy": "css",
        "query": "textarea",
        "action_kind": "type",
        "page_url_pattern": "/inbox/",
    },
)
