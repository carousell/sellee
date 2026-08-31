"""Facebook's browser contract: the Marketplace folder, the message read, the composer, the login
probe.

Two facts about this marketplace shape everything below, and both cost a day each to find.

**The seller's marketplace conversations are a folder inside Messenger, and the folder has no URL.**
`/marketplace/inbox/` looks like the right page and is not: it renders the rows scoped correctly but
carries no thread identity at all — no link, no id, no data attribute — and clicking a row opens the
conversation in place without changing `location`. Messenger's own `/messages/` carries the identity
on every row and is scoped to the wrong thing: it is the seller's personal inbox, e2ee chats
included. The one view that has both is a *folder* of Messenger, reached by clicking a row in the
chat rail, at no address of its own. So the list read here is: open `/messages/`, click that row,
and read what replaces the personal list.

**The folder only opens for a trusted click.** A click dispatched from the page does nothing —
the control listens for the real event — which is why `INBOX_FOLDER_JS` only *marks* the row and
the caller does the clicking through the browser. That split is the whole reason the adapter carries
a marking artifact and a target rather than a single "open the inbox" script.

Everything here is written against the desktop layout, which Facebook serves only above roughly
900px. Below that the folder is not reachable at all and the conversation list carries no thread
identity — see `blindness.MIN_USABLE_WIDTH_PX`, which the browser lane now enforces on every
acquisition. Selectors are class-agnostic throughout: Facebook ships hashed class names that churn
on every deploy, so nothing here matches one.
"""

from __future__ import annotations

import json

from sellee.browser.markets import jslib

# Where a listing's id sits in its permalink. Facebook names the listing a conversation is about
# only inside the opened conversation — see `PRODUCT_ID_JS` — and this is what turns that link into
# the id `reconcile.matching_items` joins on.
LISTING_ID_PATTERN = r"/marketplace/item/(\d+)"

# Rows that are Facebook talking to the seller rather than a buyer. Deliberately a backstop and not
# the scoping mechanism: what keeps personal and e2ee chats out of the lane is that they are not in
# the Marketplace folder, so nothing here has to recognise them and nothing opens them.
SYSTEM_HANDLES = frozenset({"facebook", "marketplace", "meta business support", "meta ai"})

# The attribute `INBOX_FOLDER_JS` stamps on the folder control, and the selector the caller clicks.
# One constant so the marking and the clicking cannot drift apart.
FOLDER_MARK_ATTR = "data-sellee-inbox-folder"
INBOX_FOLDER_TARGET = f"[{FOLDER_MARK_ATTR}='1']"

# Mark the control that opens the Marketplace folder, for the caller to click for real.
#
# It is located by what it is rather than by where it sits: a row in the chat rail, of row height,
# whose whole label is "Marketplace" and an age. It carries no href, no aria-label and no id — there
# is nothing more specific to match, and matching a hashed class would break on the next deploy.
#
# Answers `{marked, candidates, width, visible}`. The measurements travel because a miss here is
# indistinguishable, from the outside, from a marketplace that has changed shape — and the usual
# cause is neither: it is a window too narrow for the rail to render.
INBOX_FOLDER_JS = f"""() => {{
  const RAIL_EDGE = 500;
  let marked = false;
  let candidates = 0;
  document.querySelectorAll('div[role="button"]').forEach((el) => {{
    const r = el.getBoundingClientRect();
    if (r.left > RAIL_EDGE || r.width < 200 || r.height < 40 || r.height > 100) return;
    const text = (el.innerText || '').trim();
    if (!/^Marketplace\\b/.test(text) || text.length > 40) return;
    candidates++;
    if (marked) return;
    el.setAttribute('{FOLDER_MARK_ATTR}', '1');
    marked = true;
  }});
  return {{
    marked: marked,
    candidates: candidates,
    width: window.innerWidth,
    visible: document.visibilityState === 'visible',
  }};
}}"""

# The conversations in the Marketplace folder.
#
# Read after the caller has opened the folder, and it proves that for itself before answering: the
# rail's heading is "Chats" on the personal inbox and "Marketplace" in the folder, so a read that
# cannot see the second reports a failure rather than a list. That check is the whole safety of this
# artifact. Without it an unopened folder answers with the personal inbox, whose rows name no
# listing, and every one of them would be reported as a conversation about nothing — or, on a seller
# whose personal rows happen to be filtered out, as an empty marketplace inbox, which is the one
# answer that permanently stops the asking.
#
# Scope is belt and braces: opening the folder slides the personal list off-canvas rather than
# unmounting it, so its rows are still in the DOM at a negative x. Only on-screen rows are read.
#
# Identity and counterpart come from the row's own `aria-label` — "Group chat: <buyer> · <listing>",
# Facebook's phrasing for a marketplace thread — rather than from scraped text, so a preview
# containing " · " cannot be mistaken for the separator. A row that does not carry that shape is
# skipped and counted, never guessed at.
#
# `product_id` is deliberately null. The folder names the listing by title only, and a title is not
# something to match an item on; the id is read from the opened conversation instead. `unread` is 0
# for the same reason — the folder marks unread rows in a way that has not been captured, and a
# guess would suppress reads. Both are safe defaults: `_can_skip` errs toward opening, and the
# periodic full sweep opens everything regardless.
CONVERSATIONS_LIST_JS = """async () => {
  const RAIL_EDGE = 400;
  const SEPARATOR = ' \\u00b7 ';
  // The folder's own heading, which is what proves the folder is open. Scoped to the rail: the
  // right-hand pane shows the word "Marketplace" on the listing banner of every open conversation.
  const folderOpen = () =>
    Array.from(document.querySelectorAll('h1')).some((el) => {
      const r = el.getBoundingClientRect();
      return r.left < RAIL_EDGE && r.width > 0 && (el.innerText || '').trim() === 'Marketplace';
    });
  const rows = () =>
    Array.from(document.querySelectorAll('a[href*="/messages/t/"]')).filter((a) => {
      const r = a.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && r.left >= 0;
    });
  const read = () => {
    if (!folderOpen()) return null;
    const out = [];
    let skipped = 0;
    rows().forEach((a) => {
      const id = ((a.getAttribute('href') || '').match(/\\/t\\/(\\d+)/) || [])[1];
      const label = (a.getAttribute('aria-label') || '').replace(/^Group chat:\\s*/, '').trim();
      const at = label.indexOf(SEPARATOR);
      if (!id || at < 0) { skipped++; return; }
      // The row's second line is the preview: "You: Yes", "Kamruzzaman: Price can nego". Kept
      // whole, sender prefix included — the caller matches it as a substring.
      const lines = (a.innerText || '').trim().split('\\n').map((s) => s.trim()).filter(Boolean);
      out.push({
        thread_id: id,
        handle: label.slice(0, at).trim(),
        product_id: null,
        title: label.slice(at + SEPARATOR.length).trim(),
        unread: 0,
        last_message: lines.length > 1 ? lines[1] : '',
      });
    });
    return { conversations: out, skipped: skipped };
  };
  // The folder paints after the click returns, so the first look can still be the personal inbox.
  const deadline = Date.now() + 5000;
  let result = read();
  while (Date.now() < deadline && result === null) {
    await new Promise((r) => setTimeout(r, 250));
    result = read();
  }
  if (result === null) {
    return {
      error: 'the Marketplace folder is not open',
      rows: rows().length,
      width: window.innerWidth,
      visible: document.visibilityState === 'visible',
    };
  }
  return result;
}"""

# Which listing the open conversation is about.
#
# Facebook puts the item on a banner above the message log, as a real link, and that link is the
# only place in the whole flow where the conversation and the listing id appear together. Read once,
# when a conversation is first seen, and from then on the thread carries the item.
PRODUCT_ID_JS = """() => {
  const link = document.querySelector('a[href*="/marketplace/item/"]');
  if (!link) return { product_id: null, visible: document.visibilityState === 'visible' };
  const id = ((link.getAttribute('href') || '').match(/\\/marketplace\\/item\\/(\\d+)/) || [])[1];
  return { product_id: id || null, visible: document.visibilityState === 'visible' };
}"""

# Read the trailing message bubbles of the open conversation.
#
# Direction comes from GEOMETRY, exactly as Carousell's does: within the message log an outbound
# bubble hugs the right edge and an inbound one hugs the left. Captured live — a buyer's message at
# left 440 of a log starting at 376, ours right-aligned at 1538 of a log ending at 1584.
#
# The message text lives in `[dir="auto"]` nodes, which Facebook nests: the same string appears on a
# node and again on its child, so anything containing a descendant with identical text is dropped
# and the innermost node kept. What remains is filtered against the chrome that reads exactly like a
# short message — the banner title, the send receipts, the "started this chat" notice, and
# Facebook's own quick-reply suggestions, which are the dangerous ones: "Yes, are you interested?"
# is indistinguishable from something a person typed, and recording one would have the agent
# answering itself.
#
# The counterpart's name is dropped too, and that needs saying because it is the one lossy rule
# here: Facebook labels each run of bubbles with the sender's name, and a name label is not
# distinguishable from a one-word message by shape or position — only by being exactly the name.
# It is taken from the log's own `aria-label` ("Messages in conversation titled <buyer> ·
# <listing>") rather than assumed. A buyer whose whole message is their own name is read as a
# label and skipped; that costs one message, against reading a label as one in every conversation.
#
# Returns a list of bubbles, or `{error, logs, width, height, visible}` when no message log could be
# found — which the caller must treat as a failed read, never as a conversation with nothing in it.
# Async for the same reason Carousell's is: the messages arrive after the load event.
CONVERSATION_TAIL_JS = """async () => {
  const RECEIPTS = /^(sent|sending|delivered|seen|message sent|read|enter to send)$/i;
  const NOTICES = new RegExp(
    '(started this chat|waiting for your response|send a quick response' +
      '|you sent an attachment|view buyer profile)',
    'i'
  );
  const logs = () =>
    Array.from(document.querySelectorAll('[role="log"]')).filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 200 && r.height > 100;
    });
  const read = () => {
    const log = logs()[0];
    if (!log) return null;
    const lr = log.getBoundingClientRect();
    // The conversation's own title, and the counterpart's name inside it — the two strings that
    // appear in the log as chrome and would otherwise read as messages.
    const title = (log.getAttribute('aria-label') || '')
      .replace(/^Messages in conversation titled\\s*/i, '')
      .trim();
    const at = title.indexOf(' \\u00b7 ');
    const counterpart = at < 0 ? '' : title.slice(0, at).trim();
    const nodes = Array.from(log.querySelectorAll('[dir="auto"]'));
    const out = [];
    nodes.forEach((el) => {
      const text = (el.innerText || '').trim();
      if (!text) return;
      // Facebook nests the same string on a node and its child. Keep the innermost.
      const inner = el.querySelectorAll('[dir="auto"]');
      for (let i = 0; i < inner.length; i++) {
        if ((inner[i].innerText || '').trim() === text) return;
      }
      if (RECEIPTS.test(text) || NOTICES.test(text)) return;
      if (counterpart && text === counterpart) return;
      if (title && text.indexOf(title) === 0) return;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return;
      const fromLeft = r.left - lr.left;
      const fromRight = lr.right - r.right;
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
  while (Date.now() < deadline && (!Array.isArray(result) || result.length === 0)) {
    await new Promise((r) => setTimeout(r, 250));
    result = read();
  }
  if (!Array.isArray(result)) {
    return {
      error: 'no_message_log',
      logs: logs().length,
      width: window.innerWidth,
      height: window.innerHeight,
      visible: document.visibilityState === 'visible',
    };
  }
  return result;
}"""

# Is the seller logged in? Three-state, and it must never answer logged_out on thin evidence: a
# false logged_out tells a signed-in seller to re-authenticate and stops their market.
#
# Only the password field proves logged_out — Facebook renders its login form on every page it
# refuses — and only a control that exists solely for a signed-in account proves logged_in. A
# logged-out visitor gets the marketplace nav too, so that is deliberately not the marker; the chat
# rail is.
LOGIN_JS = """() => {
  try {
    if (document.querySelector('input[name="pass"], input[type="password"]')) {
      return { state: 'logged_out' };
    }
    const rail = !!document.querySelector('a[href*="/messages/t/"]');
    const compose = !!document.querySelector('[aria-label="New message"], [role="textbox"]');
    if (rail || compose) return { state: 'logged_in' };
    const text = (document.body && document.body.innerText) || '';
    if (/\\bLog in to Facebook\\b|\\bLog Into Facebook\\b/i.test(text)) {
      return { state: 'logged_out' };
    }
    return { state: 'unknown' };
  } catch (e) {
    return { state: 'unknown' };
  }
}"""

# Where the seller's own listings actually are.
#
# `/marketplace/you/selling` looks like the page and is not: its cards carry the title, the price
# and a per-card "In stock", but no listing id anywhere — no link, no data attribute — so nothing
# read there can be joined to a conversation or recorded as a listing URL. The seller's public
# Marketplace profile carries the same listings as real `/marketplace/item/<id>` links, and it is
# reached from the selling page by a link whose href holds the seller's own account id. That id is
# a fact about *them*, not about Facebook, so it is not in the registry and not stored: it is read
# from the page each time, which costs one hop and nothing else.
#
# Answers `{url}` — the address to read the listings from — or `{url: null}` when the link is not
# there, which the caller reports rather than reading the wrong page.
MY_LISTINGS_ENTRY_JS = """() => {
  const link = document.querySelector('a[href*="/marketplace/profile/"]');
  const href = link ? link.getAttribute('href') : null;
  return {
    url: href || null,
    width: window.innerWidth,
    visible: document.visibilityState === 'visible',
  };
}"""

_MY_LISTINGS_TEMPLATE = """async () => {
  const LISTING_HREF = new RegExp(__LISTING_ID_RE__);
  const parsePrice = __PARSE_PRICE__;
  // The seller's own listings, and nothing else on the page. Their profile renders under a heading
  // that names them ("Jerry Neo's listings"), and Facebook interleaves a "Today's picks" grid of
  // OTHER people's listings down the same page — at overlapping vertical positions, so neither the
  // heading's offset nor a column band separates them. The first ancestor of that heading which
  // holds any listing link is the seller's grid and holds only it, which is the one rule here that
  // matters: read the wrong container and the survey offers to relist strangers' items.
  const scope = () => {
    const heading = Array.from(document.querySelectorAll('h1,h2,h3,[role="heading"]'))
      .find((el) => /listings$/i.test((el.innerText || '').trim()));
    if (!heading) return null;
    for (let node = heading; node; node = node.parentElement) {
      if (node.querySelector('a[href*="/marketplace/item/"]')) return node;
    }
    return null;
  };
  const cards = (root) => {
    const seen = new Map();
    root.querySelectorAll('a[href*="/marketplace/item/"]').forEach((a) => {
      const href = a.href || a.getAttribute('href') || '';
      const match = href.match(LISTING_HREF);
      if (match && !seen.has(match[1])) seen.set(match[1], a);
    });
    return seen;
  };

  const root = scope();
  if (root === null) {
    return {
      error: 'no scoped listings container',
      headings: document.querySelectorAll('h1,h2,h3,[role="heading"]').length,
      width: window.innerWidth,
      visible: document.visibilityState === 'visible',
    };
  }
  // The page's own count of what is live — the only thing that can tell a partial render from a
  // small inventory, and what stops an ask-once survey closing on 12 of 17.
  const bodyText = (document.body && document.body.innerText) || '';
  const active = Number((bodyText.match(/(\\d+)\\s+active listings?/i) || [])[1]) || 0;

  // The grid loads lazily, and `window.scrollTo` does not drive it — bringing the last card into
  // view does. Stop when the count has stopped growing, or when it reaches the page's own tally.
  let previous = -1;
  let settled = 0;
  for (let pass = 0; pass < 30 && settled < 3; pass++) {
    const found = cards(root);
    if (found.size === previous) settled++;
    else { settled = 0; previous = found.size; }
    if (active && found.size >= active) break;
    const all = Array.from(found.values());
    if (all.length) all[all.length - 1].scrollIntoView({ block: 'end' });
    await new Promise((r) => setTimeout(r, 400));
  }

  const listings = [];
  let dropped = 0;
  cards(root).forEach((a, id) => {
    // A card reads price, title, location — one line each. The title is taken by position rather
    // than by guessing which line is not a price, because a listing may legitimately be titled
    // something that parses as one.
    const lines = (a.innerText || '').trim().split('\\n').map((s) => s.trim()).filter(Boolean);
    const priceText = lines[0] || '';
    const title = lines[1] || '';
    const price = parsePrice(priceText);
    if (!title || !isFinite(price)) { dropped++; return; }
    listings.push({
      listing_id: id,
      url: a.href || a.getAttribute('href') || '',
      title: title,
      price: price,
      price_text: priceText,
    });
  });

  return {
    listings: listings,
    active_count: active,
    dropped: dropped,
    unreadable: dropped,
    // Never report a partial grid as the seller's whole inventory. The tally is the page's own
    // statement of how many are live; short of it, this read did not finish.
    truncated: active > 0 && listings.length + dropped < active,
    visible: document.visibilityState === 'visible',
  };
}"""

MY_LISTINGS_JS = _MY_LISTINGS_TEMPLATE.replace(
    "__LISTING_ID_RE__", json.dumps(LISTING_ID_PATTERN)
).replace("__PARSE_PRICE__", jslib.PARSE_PRICE_JS)

# One listing's own page, read at adoption time.
#
# There is no JSON-LD — Facebook publishes none for a marketplace item — so this is DOM work, and
# that makes liveness the dangerous field. `active` is true ONLY on a positive in-stock marker: a
# reader that cannot prove a listing is live must say it is not, because the cost of the other
# mistake is relisting something the seller already sold.
#
# The item's own block is located by the title rather than by position: navigating to an item URL
# renders the marketplace's whole chrome — nav, categories, notifications — around a panel, and
# `document.body.innerText` begins with all of it.
#
# Photographs are taken only from images Facebook labels "Product photo of …". The page also
# carries a grid of similar listings from other sellers at the same CDN hosts, and their pictures
# are indistinguishable from the item's own by host, size or position.
LISTING_DETAIL_JS = """async () => {
  const parsePrice = __PARSE_PRICE__;
  // Facebook's own section labels inside the panel. Walking up stops at the first ancestor whose
  // opening line is NOT one of these, which is the ancestor whose opening line is the title.
  const SECTIONS = ['Details', 'Condition', 'Description', 'Seller information'];
  // The panel's own footer, which sits where a description would be when there is not one.
  const TRAILER = /Location is approximate|^See (more|less)$|^Edit$|^Message$/i;
  const read = () => {
    // Anchored on the "Condition" label rather than on the title, because the title cannot be
    // recognised without already knowing it: navigating to an item renders the marketplace's whole
    // chrome around the panel, and the biggest heading on the page is the word "Marketplace".
    const anchor = Array.from(document.querySelectorAll('span,div')).find(
      (el) => el.children.length === 0 && (el.innerText || '').trim() === 'Condition'
    );
    if (!anchor) return null;
    let panel = null;
    for (let node = anchor, i = 0; node && i < 18; node = node.parentElement, i++) {
      const first = ((node.innerText || '').trim().split('\\n')[0] || '').trim();
      if (first && SECTIONS.indexOf(first) === -1) { panel = node; break; }
    }
    if (panel === null) return null;
    const lines = (panel.innerText || '').split('\\n').map((s) => s.trim()).filter(Boolean);
    const title = lines[0] || '';
    // The price is the line under the title, and only falls back to a scan if that is not one —
    // a title containing a number must never be read as the asking price.
    const priceText = isFinite(parsePrice(lines[1] || '')) && /\\d/.test(lines[1] || '')
      ? lines[1]
      : (lines.slice(1).find((l) => isFinite(parsePrice(l)) && /\\d/.test(l)) || '');
    const conditionAt = lines.indexOf('Condition');
    const photos = Array.from(document.querySelectorAll('img'))
      .filter((i) => /^Product photo of /i.test(i.getAttribute('alt') || ''))
      .map((i) => i.src || '')
      .filter(Boolean);
    // Liveness, and the whole reason this fails closed. "In stock" is Facebook's own words on a
    // live listing; a sold one says so instead, and anything we cannot read says nothing.
    const body = (panel.innerText || '');
    const live = /\\bIn stock\\b/i.test(body);
    const sold = /\\b(Sold|Out of stock|Pending|no longer available)\\b/i.test(body);
    return {
      active: live && !sold,
      title: title,
      // The line under the condition value, when there is one. A listing with no description at
      // all runs straight on to the panel's footer, so that footer is named and refused rather
      // than being adopted as the seller's own words.
      description: conditionAt >= 0 && !TRAILER.test(lines[conditionAt + 2] || '')
        ? (lines[conditionAt + 2] || '')
        : '',
      price: parsePrice(priceText),
      price_text: priceText,
      currency: (priceText.match(/[A-Z]{3}/) || [''])[0],
      condition: conditionAt >= 0 ? (lines[conditionAt + 1] || '') : '',
      photo_urls: Array.from(new Set(photos)).slice(0, 20),
    };
  };
  // The panel arrives after the load event, like every other read here.
  const deadline = Date.now() + 5000;
  let result = read();
  while (Date.now() < deadline && (result === null || !result.title)) {
    await new Promise((r) => setTimeout(r, 250));
    result = read();
  }
  return result;
}""".replace("__PARSE_PRICE__", jslib.PARSE_PRICE_JS)

# The reply composer, as shipped defaults under the heal cache.
#
# `page_url_pattern` is the conversation URL, not the marketplace inbox: a send happens on
# `/messages/t/<id>/`, and an earlier version of this pinned it to `/marketplace/t/` — a path that
# does not exist — so every send failed closed before a word was typed.
#
# Unlike Carousell there IS a send control to click, and it is only present once the composer holds
# text: the button at the end of the composer row is "Send a like" on an empty box and "Press enter
# to send" on a full one. Clicking it costs the seller nothing — no window focus taken, no
# `isTrusted: false` event on their account — which is why this market has no submit JS.
COMPOSER_DEFAULTS = (
    {
        "step": "message_box",
        "strategy": "css",
        "query": '[role="textbox"][contenteditable="true"]',
        "action_kind": "type",
        "page_url_pattern": "/messages/t/",
    },
    {
        "step": "send_button",
        "strategy": "css",
        "query": '[aria-label="Press enter to send"]',
        "action_kind": "click",
        "page_url_pattern": "/messages/t/",
    },
)
