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

import json

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

# What the seller already has listed, read off their own manage-listings page.
#
# The DOM, not an API, and unusually this is the *stable* choice: the page renders a real
# `<table>` with a `<thead>`, so the columns are named by the page itself and located by their
# heading rather than by position or by one of Carousell's hashed classes. (The endpoint the page
# fetches this from refuses a plain GET, and the one that answers is an insights feed carrying no
# listing at all — so the table is also the only thing that actually holds the answer.)
#
# Two facts make this safe to adopt from, and both are checked rather than assumed:
#
#   1. **These are live listings.** The page keeps sold and expired listings behind other tabs, and
#      those tabs are buttons carrying no selected state — so instead of trusting which tab is
#      showing, this reads the "N Active" count and compares it with the rows it found. A view that
#      is not the active one has a different number of rows, and answers `{error}` rather than a
#      list. Rows carrying a status badge are dropped as a second, independent guard.
#   2. **A price we can read.** A listing with no parseable price is dropped and counted, never
#      returned: carousell.ai refuses an item without one, so asking the seller about it would only
#      promise something that could never be kept.
#
# Returns `{listings: [...], active_count, dropped, unreadable, truncated}` or `{error: …}`. The
# distinction matters here more than anywhere: an empty list means "you have nothing listed", which
# is the one answer that stops us ever asking. `unreadable` is what keeps a page we could not parse
# from arriving as that answer — see the caller.
#
# A price is read from the text the page renders, and the thousands separator is not the same on
# every regional site, so neither `,` nor `.` can be assumed to be the decimal point. Kept as its
# own function because it is the one piece here that is worth testing directly.
PARSE_PRICE_JS = """(text) => {
  const trimmed = String(text || '').replace(/[^0-9.,]/g, '');
  if (!trimmed) return NaN;
  const lastDot = trimmed.lastIndexOf('.');
  const lastComma = trimmed.lastIndexOf(',');
  if (lastDot >= 0 && lastComma >= 0) {
    // Both appear, so the later one is the decimal point and the other groups thousands:
    // "S$1,299.00" one way, "Rp1.299,00" the other.
    const decimal = lastDot > lastComma ? '.' : ',';
    const grouping = decimal === '.' ? ',' : '.';
    return Number(trimmed.split(grouping).join('').replace(decimal, '.'));
  }
  const sep = lastDot >= 0 ? '.' : (lastComma >= 0 ? ',' : '');
  if (!sep) return Number(trimmed);
  // Only one kind of separator, so its job has to be inferred: more than one of them, or exactly
  // three digits after the last one, means it groups thousands ("1.500.000", "1,299"). Anything
  // else is a decimal point ("40.00", "1,5").
  const parts = trimmed.split(sep);
  const groups = parts.length > 2 || parts[parts.length - 1].length === 3;
  return Number(groups ? parts.join('') : parts.join('.'));
}"""

_MY_LISTINGS_TEMPLATE = """async () => {
  const LISTING_HREF = new RegExp(__LISTING_ID_RE__);
  // Text a row shows when it is anything other than live. Matched against the row's own badge
  // text, not searched for inside it, so a listing called "Sold Out Records Tee" is not dropped.
  const BADGES = ['sold', 'reserved', 'expired', 'deleted', 'under review', 'rejected', 'inactive'];
  const clean = (el) => ((el && el.innerText) || '').replace(/\\s+/g, ' ').trim();
  // The title cell also carries the row's own notes under the title ("Bumped 16 days ago"), which
  // are separate blocks — so the title is its first line, not all of its text.
  const firstLine = (el) =>
    (((el && el.innerText) || '').split('\\n').map((s) => s.trim()).filter(Boolean)[0] || '');
  const parsePrice = __PARSE_PRICE__;

  const readRows = (table, priceIndex) => {
    const rows = [];
    // Counted apart because they mean opposite things. A row behind a badge is not for sale, so
    // dropping it is the answer; a row whose price will not parse is a row we failed to read, and
    // if that is every row the page has, reporting an empty list would tell the seller they have
    // nothing listed.
    let inactive = 0;
    let unreadable = 0;
    table.querySelectorAll('tbody tr').forEach((tr) => {
      const link = tr.querySelector('a[href*="/p/"]');
      if (!link) return;
      const href = link.getAttribute('href') || '';
      const match = LISTING_HREF.exec(href.split('?')[0]);
      if (!match) return;
      const cells = Array.from(tr.children);
      const badge = cells.some((td) =>
        Array.from(td.querySelectorAll('*'))
          .filter((el) => el.children.length === 0)
          .some((el) => BADGES.includes(clean(el).toLowerCase()))
      );
      if (badge) { inactive++; return; }
      // The title cell is the one holding the listing link, so it never has to be counted to.
      let titleCell = link;
      while (titleCell && titleCell.tagName !== 'TD') titleCell = titleCell.parentElement;
      const title = firstLine(titleCell);
      const priceText = priceIndex >= 0 && cells[priceIndex] ? clean(cells[priceIndex]) : '';
      const price = parsePrice(priceText);
      if (!title || !isFinite(price) || price <= 0) { unreadable++; return; }
      rows.push({
        listing_id: match[1],
        url: new URL(href, location.origin).href,
        title: title.slice(0, 200),
        price: price,
        price_text: priceText.slice(0, 40),
      });
    });
    return { rows: rows, dropped: inactive + unreadable, unreadable: unreadable };
  };

  try {
    const table = document.querySelector('table');
    if (!table) return { error: 'no listings table on the page' };
    const headings = Array.from(table.querySelectorAll('th'));
    const priceIndex = headings.findIndex((th) => /^price$/i.test(clean(th)));
    if (priceIndex < 0) return { error: 'no price column in the listings table' };

    let activeCount = null;
    Array.from(document.querySelectorAll('button, a, [role="tab"]')).forEach((el) => {
      const hit = /^(\\d+)\\s+active$/i.exec(clean(el));
      if (hit && activeCount === null) activeCount = Number(hit[1]);
    });
    if (activeCount === null) return { error: 'could not read the active listing count' };

    // Load what is not rendered yet. An account with more listings than one screen holds must not
    // be read as having fewer, so this keeps going until the count reaches the page's own tally —
    // and gives up only after several rounds add nothing, because a fetch in flight looks exactly
    // like a list that has ended. A read that still falls short says so with `truncated`, and the
    // caller treats that as a look it could not complete rather than as the whole answer.
    let seen = readRows(table, priceIndex);
    let quiet = 0;
    for (let round = 0; round < 20 && seen.rows.length + seen.dropped < activeCount; round++) {
      window.scrollTo(0, document.body.scrollHeight);
      await new Promise((r) => setTimeout(r, 700));
      const again = readRows(table, priceIndex);
      quiet = again.rows.length + again.dropped === seen.rows.length + seen.dropped ? quiet + 1 : 0;
      seen = again;
      if (quiet >= 3) break;
    }

    const total = seen.rows.length + seen.dropped;
    if (total > activeCount) {
      // More rows than there are active listings: this is not the active view, so every row is
      // suspect. Saying so is the whole point — adopting from here would take in sold listings.
      return { error: 'showing ' + total + ' rows for ' + activeCount + ' active listings' };
    }
    return {
      listings: seen.rows,
      active_count: activeCount,
      dropped: seen.dropped,
      unreadable: seen.unreadable,
      truncated: total < activeCount,
    };
  } catch (e) {
    return { error: String(e).slice(0, 200) };
  }
}"""

# The id pattern is injected rather than written twice: this and `listing_id_pattern` must agree
# about what a listing id is, and json.dumps gives a JS string literal whose escapes survive. The
# price parser is injected for the same reason in reverse — so it can be exercised on its own.
MY_LISTINGS_JS = _MY_LISTINGS_TEMPLATE.replace(
    "__LISTING_ID_RE__", json.dumps(LISTING_ID_PATTERN)
).replace("__PARSE_PRICE__", PARSE_PRICE_JS)

# One listing's own page, read at adoption time — the fields, the photographs, and whether it is
# still for sale.
#
# This one is schema.org JSON-LD rather than DOM: Carousell publishes a `Product` block for search
# engines, which makes it a contract with somebody other than us, and it carries every field an
# adopted item needs already typed — including the currency as a code, so no symbol ever has to be
# mapped to one, and the full-size photograph URLs rather than the index page's thumbnails.
#
# `availability` is the field that matters most. A sold listing reports `schema.org/SoldOut` where a
# live one reports `InStock`, and it is the *only* reliable signal: the rendered page shows no
# "sold" text anywhere a reader could find it. Without this check, a seller who taps yes on a list
# that has aged would have sold items relisted for them.
#
# `itemCondition` is deliberately ignored — Carousell reports `NewCondition` for every listing,
# including one whose own page says "Lightly used" — so the condition is read from the visible
# details instead, and is simply absent when it cannot be found.
#
# Returns null when the page carries no Product block, which the caller must treat as "could not
# read", never as "not for sale".
LISTING_DETAIL_JS = """() => {
  const clean = (el) => ((el && el.innerText) || '').replace(/\\s+/g, ' ').trim();
  const product = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
    .map((s) => { try { return JSON.parse(s.textContent); } catch (e) { return null; } })
    .find((o) => o && o['@type'] === 'Product');
  if (!product) return null;

  const offers = product.offers || {};
  const availability = String(offers.availability || '');
  const price = Number(offers.price);
  const images = (Array.isArray(product.image) ? product.image : [product.image])
    .filter((u) => typeof u === 'string' && u.startsWith('https://'));

  // The visible "Condition" row, as a label followed by its value. Best-effort by design: a missing
  // condition costs the relisted copy a detail, where a wrong one describes the seller's goods
  // incorrectly to a buyer.
  let condition = null;
  const leaves = Array.from(document.querySelectorAll('div, span, p, dt, dd, li'))
    .filter((el) => el.children.length === 0);
  for (let i = 0; i < leaves.length - 1; i++) {
    if (/^condition$/i.test(clean(leaves[i]))) {
      const value = clean(leaves[i + 1]);
      if (value && value.length <= 40 && !/^condition$/i.test(value)) condition = value;
      break;
    }
  }

  return {
    active: /InStock$/i.test(availability),
    availability: availability,
    title: String(product.name || '').slice(0, 200),
    description: String(product.description || '').slice(0, 4000),
    price: isFinite(price) && price > 0 ? price : null,
    currency: String(offers.priceCurrency || '') || null,
    condition: condition,
    photo_urls: images.slice(0, 20),
  };
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
