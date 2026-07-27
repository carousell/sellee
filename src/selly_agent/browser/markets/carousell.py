"""Carousell's browser contract: the JS artifacts, the composer selectors, the login probe.

The three JS functions here are the layer's only DOM knowledge. Each is written class-agnostically —
Carousell ships hashed CSS classes that churn on every deploy, so nothing keys on one. They
locate by role, by href shape, and (for message direction) by geometry.
"""

from __future__ import annotations

PUBLISH_SKILL = "listing-flow-carousell"

# Rows that are Carousell talking to the seller, not a buyer: the platform assistant and its
# promotional accounts. They are never conversations to answer.
SYSTEM_HANDLES = frozenset({"carousell_assistant", "selltocarousell_mobiles", "carousell"})

# Enumerate the inbox's conversation rows.
#
# Returns null — not [] — when this is not the inbox. An empty list is a claim that the inbox is
# clear, and making that claim from a listing page would strand an unread buyer; null says "I cannot
# see" instead, which the caller treats as a failed read (a market that cannot be seen must never
# look like a market with no news).
#
# A row's thread id comes from a link to the conversation. Rows without one are reported with a null
# id: they can still be matched against threads we already track, but they can never create one,
# because without an id there is no durable key and no page to open.
DISCOVERY_JS = """() => {
  if (!/\\/inbox/.test(location.pathname)) return null;
  const idOf = (href) => {
    const m = (href || '').match(/\\/inbox\\/([A-Za-z0-9_-]+)\\/?/);
    return m ? m[1] : null;
  };
  const rows = Array.from(document.querySelectorAll('div[role="button"], li, a'))
    .filter((el) => {
      const text = (el.textContent || '').trim();
      return text.length > 6 && (el.querySelector('img') || idOf(el.getAttribute('href')));
    });
  const seen = new Set();
  const out = [];
  for (const el of rows) {
    const text = (el.textContent || '').trim().replace(/\\s+/g, ' ');
    let id = idOf(el.getAttribute && el.getAttribute('href'));
    if (!id) {
      const link = el.querySelector('a[href*="/inbox/"]');
      if (link) id = idOf(link.getAttribute('href'));
    }
    if (!id) {
      const parent = el.closest && el.closest('a[href*="/inbox/"]');
      if (parent) id = idOf(parent.getAttribute('href'));
    }
    const key = id || text;
    if (seen.has(key)) continue;
    seen.add(key);
    // Unread is a lone count badge ("2", or the capped "9+") somewhere in the row.
    const unread = Array.from(el.querySelectorAll('span,div'))
      .some((n) => /^\\d{1,3}\\+?$/.test((n.textContent || '').trim()));
    out.push({ thread_id: id, text: text.slice(0, 300), unread: unread });
    if (out.length >= 40) break;
  }
  return out;
}"""

# Read the trailing message bubbles of the open conversation.
#
# Direction comes from GEOMETRY, never from CSS classes: an outbound bubble hugs the right edge of
# the chat pane and an inbound one hugs the left. The 35%-width cut drops the inbox list rail on the
# left of the page. Rows that sit roughly centred are system banners and offer widgets, reported as
# "center" so the caller can ignore them rather than mistake one for a message.
TAIL_JS = """() => {
  const W = window.innerWidth;
  const cut = W * 0.35;
  const out = [];
  const ps = document.querySelectorAll('p');
  for (let i = 0; i < ps.length; i++) {
    const el = ps[i];
    if (el.children.length > 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.left <= cut) continue;
    const text = (el.textContent || '').trim();
    if (!text) continue;
    const fromLeft = r.left - cut;
    const fromRight = W - r.right;
    let side = 'center';
    if (fromRight < fromLeft * 0.6) side = 'out';
    else if (fromLeft < fromRight * 0.6) side = 'in';
    out.push({ text: text.slice(0, 300), side: side, y: Math.round(r.top) });
  }
  out.sort((a, b) => a.y - b.y);
  return out;
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

# The reply composer, as shipped defaults under the heal cache. A chat page has one message box and
# one send control, so these stay class-agnostic; when Carousell moves either, the resolve finds
# nothing, the send fails closed before any click, and the healed selector is what the cache learns.
COMPOSER_DEFAULTS = (
    {
        "step": "message_box",
        "strategy": "css",
        "query": "textarea",
        "action_kind": "type",
        "page_url_pattern": "/inbox/",
    },
    {
        "step": "send_button",
        "strategy": "css",
        "query": 'button[aria-label="Send"], button[type="submit"]',
        "action_kind": "click",
        "page_url_pattern": "/inbox/",
    },
)
