"""Facebook's browser contract: the Marketplace folder, the message read, the composer, the login
probe.

Two facts shape everything below.

**The seller's marketplace conversations are a folder inside Messenger, and the folder has no URL.**
`/marketplace/inbox/` renders rows scoped correctly but carries no thread identity; `/messages/`
carries identity but is the seller's personal inbox, e2ee chats included. The one view with both
is a folder of Messenger reached by clicking a row in the chat rail. So the list read here is:
open `/messages/`, click that row, read what replaces the personal list.

**The folder only opens for a trusted click.** A click dispatched from the page does nothing, so
`INBOX_FOLDER_JS` only *marks* the row and the caller clicks through the browser — which is why
the adapter carries a marking artifact and a target rather than one "open the inbox" script.

Everything here is written against the desktop layout, which Facebook serves only above roughly
900px (`MIN_USABLE_WIDTH_PX`). Selectors are class-agnostic throughout: Facebook's hashed class
names churn on every deploy.
"""

from __future__ import annotations

import json

from sellee.browser.markets import jslib, publishing

# Where a listing's id sits in its permalink. Facebook names the listing a conversation is about
# only inside the opened conversation — see `PRODUCT_ID_JS` — and this is what turns that link into
# the id `reconcile.matching_items` joins on.
LISTING_ID_PATTERN = r"/marketplace/item/(\d+)"

# Below this Facebook serves its narrow layout, which none of the readers here can parse — the
# number is Facebook's own responsive breakpoint, not a guess.
MIN_USABLE_WIDTH_PX = 900

# Rows that are Facebook talking to the seller rather than a buyer. Deliberately a backstop and not
# the scoping mechanism: what keeps personal and e2ee chats out of the lane is that they are not in
# the Marketplace folder, so nothing here has to recognise them and nothing opens them.
SYSTEM_HANDLES = frozenset({"facebook", "marketplace", "meta business support", "meta ai"})

# What Facebook puts in the row when a message has been withdrawn, or the sender's account is gone.
# The row's own preview saying "nothing to show" is an answer, not a page that changed shape: an
# empty tail on such a row must not be counted as blindness, and the row is never worth opening.
EMPTY_PREVIEW_PATTERN = r"^\s*message unavailable\b"

# A row's trailing relative-time token — "2m", "1h", "Just now", "Yesterday", "10:11 AM", "12/05".
# Messenger's clock advances it on a message that has not changed, so a row comparison must not
# read it as the conversation changing.
ROW_CLOCK_PATTERN = (
    r"(?:\d{1,2}:\d{2}\s?(?:AM|PM)?|\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d+[smhdw]"
    r"|just now|yesterday|today|mon|tue|wed|thu|fri|sat|sun)\s*$"
)

# What Facebook's verification wall is: a PIN prompt in front of encrypted chats, hiding the
# messages entirely. The wall is reported by the list artifact (`blocked: 'verify'`); the wording
# is this market's because the thing being asked for — a Messenger PIN — is.
VERIFY_NOTICE = (
    "I can't read your {name} messages — {name} is asking for your PIN before it will show them. "
    "That one's yours to answer: open my Chrome{where}, enter it there, and I'll pick them up on "
    "my next look. Until then your {name} app has anything I've missed."
)

# The attribute `INBOX_FOLDER_JS` stamps on the folder control, and the selector the caller clicks.
# One constant so the marking and the clicking cannot drift apart.
FOLDER_MARK_ATTR = "data-sellee-inbox-folder"
INBOX_FOLDER_TARGET = f"[{FOLDER_MARK_ATTR}='1']"

# Mark the control that opens the Marketplace folder, for the caller to click for real. Located by
# what it is — a rail row whose whole label is "Marketplace" and an age; it carries no href, no
# aria-label, no id. Answers `{marked, candidates, width, visible}`; the measurements travel
# because the usual miss is a window too narrow for the rail to render, not a marketplace that
# changed shape.
INBOX_FOLDER_JS = f"""() => {{
  const RAIL_EDGE = 500;
  // Whether the folder is already open, by the same heading the list artifact proves itself with.
  // The control's `aria-pressed` reads "true" while the rail still says "Chats".
  const alreadyOpen = Array.from(document.querySelectorAll('h1')).some((el) => {{
    const r = el.getBoundingClientRect();
    return r.left < 400 && r.width > 0 && (el.innerText || '').trim() === 'Marketplace';
  }});
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
    already_open: alreadyOpen,
    candidates: candidates,
    width: window.innerWidth,
    visible: document.visibilityState === 'visible',
  }};
}}"""

# The conversations in the Marketplace folder.
#
# Proves the folder is open before answering: the rail heading is "Chats" on the personal inbox and
# "Marketplace" in the folder, and without that check an unopened folder would answer with the
# personal inbox — whose rows name no listing, or may read as an empty marketplace inbox, which is
# the one answer that permanently stops the asking.
#
# Opening the folder slides the personal list off-canvas rather than unmounting it, so only
# on-screen rows are read. Identity and counterpart come from the row's `aria-label` ("Group chat:
# <buyer> · <listing>") rather than scraped text, so a preview containing " · " cannot be mistaken
# for the separator.
#
# `product_id` is null on purpose: the folder names the listing by title only, and a title is never
# matched on — the id is read from the opened conversation. `unread` is 0 because the folder's
# unread marker has not been captured and a guess would suppress reads; `_can_skip` errs toward
# opening and the full sweep opens everything regardless.
CONVERSATIONS_LIST_JS = """async () => {
  const RAIL_EDGE = 400;
  const SEPARATOR = ' \\u00b7 ';
  // The folder's own heading. Scoped to the rail: the right-hand pane shows the word
  // "Marketplace" on the listing banner of every open conversation.
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
  // The folder loads a screenful at a time, so a plain read answers with the most recent handful
  // and looks exactly like a seller with a handful of buyers. `window.scrollTo` does not drive
  // this list — bringing the last row into view does.
  const loadAll = async () => {
    let previous = -1;
    let settled = 0;
    for (let pass = 0; pass < 30 && settled < 3; pass++) {
      const found = rows();
      if (found.length === previous) settled++;
      else { settled = 0; previous = found.length; }
      if (found.length) found[found.length - 1].scrollIntoView({ block: 'end' });
      await new Promise((r) => setTimeout(r, 400));
    }
  };
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
  // Facebook can put a wall in front of the messages rather than refusing us: a PIN prompt for
  // encrypted chats, or a "confirm it's you" interstitial. From the lane it is indistinguishable
  // from Facebook declining to hand over the list, and the seller would be told to check a login
  // that is working perfectly. Deliberately hard to trigger — a phrase AND a prompt-shaped
  // element — because the words alone appear in buyers' own messages.
  const verifyWall = () => {
    const text = (document.body.innerText || '').toLowerCase();
    const asks = [
      'enter your pin',
      'enter pin',
      'confirm your pin',
      'restore your chats',
      "confirm it's you",
      'confirm your identity',
    ];
    if (!asks.some((phrase) => text.includes(phrase))) return '';
    const field = document.querySelector(
      'input[type="password"], input[autocomplete*="one-time"], input[inputmode="numeric"]'
    );
    return field || document.querySelector('[role="dialog"]') ? 'verify' : '';
  };
  // Back up to the newest end: `loadAll` finishes at the OLDEST row, and Messenger unmounts rows
  // far outside the viewport, so a read taken there is missing the most recent conversations
  // entirely.
  const scrollToTop = async () => {
    let previous = null;
    let settled = 0;
    for (let pass = 0; pass < 30 && settled < 3; pass++) {
      const found = rows();
      if (!found.length) break;
      if (found[0] === previous) settled++;
      else { settled = 0; previous = found[0]; }
      found[0].scrollIntoView({ block: 'start' });
      await new Promise((r) => setTimeout(r, 400));
    }
  };
  // One read is one window onto the list, never the whole of it: two reads from opposite ends,
  // merged on the conversation id. The top read goes first — the folder is newest-first, and that
  // is the order the caller should see.
  const merge = (top, bottom) => {
    const parts = [top, bottom].filter((r) => r && Array.isArray(r.conversations));
    if (!parts.length) return null;
    const seen = {};
    const out = [];
    let skipped = 0;
    parts.forEach((part) => {
      skipped += part.skipped || 0;
      part.conversations.forEach((row) => {
        if (seen[row.thread_id]) return;
        seen[row.thread_id] = true;
        out.push(row);
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
  if (result !== null) {
    await loadAll();
    const bottom = read();
    await scrollToTop();
    result = merge(read(), bottom);
  }
  if (result === null) {
    return {
      error: 'the Marketplace folder is not open',
      rows: rows().length,
      width: window.innerWidth,
      visible: document.visibilityState === 'visible',
      blocked: verifyWall(),
    };
  }
  return result;
}"""

# Which listing the open conversation is about.
#
# Facebook puts the item on a banner above the message log, as a real link, and that link is the
# only place where the conversation and the listing id appear together. Read once, when a
# conversation is first seen; from then on the thread carries the item.
PRODUCT_ID_JS = """async () => {
  const read = () => {
    const link = document.querySelector('a[href*="/marketplace/item/"]');
    if (!link) return null;
    const id = ((link.getAttribute('href') || '').match(/\\/marketplace\\/item\\/(\\d+)/) || [])[1];
    return id || null;
  };
  // Polled: the banner is fetched after the load event, so a synchronous look at a freshly
  // navigated tab finds no link — and a missing id is `unknown_listing`, which is silence.
  const deadline = Date.now() + 8000;
  let id = read();
  while (Date.now() < deadline && id === null) {
    await new Promise((r) => setTimeout(r, 250));
    id = read();
  }
  return { product_id: id, visible: document.visibilityState === 'visible' };
}"""

# Read the trailing message bubbles of the open conversation.
#
# Direction comes from GEOMETRY, as Carousell's does: an outbound bubble hugs the right edge of the
# log, an inbound one the left. Message text lives in `[dir="auto"]` nodes, which Facebook nests —
# the same string on a node and its child — so innermost nodes are kept. What remains is filtered
# against the chrome that reads like a short message: the banner title, send receipts, the "started
# this chat" notice, and Facebook's quick-reply suggestions, which are the dangerous ones — read as
# a message, "Yes, are you interested?" would have the agent answering itself.
#
# The counterpart's name is the one lossy rule here: a name label is not distinguishable from a
# one-word message except by being exactly the name (taken from the log's own `aria-label`). A
# buyer whose whole message is their own name is skipped — one message, against mis-reading a
# label in every conversation.
#
# Returns a list of bubbles, or `{error, logs, width, height, visible}` when no message log could
# be found — a failed read, never a conversation with nothing in it.

# Whether one line of the log is Facebook's furniture rather than something somebody said.
#
# Its own function because it is worth testing directly: every rule in it was written from a line
# that appeared in a real thread, and the cost of getting one wrong runs both ways — too loose
# journals Facebook's words as the buyer's, too tight deletes something they said. The timestamp
# rule needs a month or weekday, not just a time, or it eats a buyer answering "8:30pm".
CHROME_LINE_JS = r"""(text) => {
  const line = String(text || '').trim();
  // Anchored exact forms, plus the relative receipts Facebook writes under a bubble ("Sent 5m
  // ago"), which change on every read — the shape that makes a settled conversation look like it
  // keeps speaking.
  const RECEIPT_WORDS = 'sent|sending|delivered|seen|read';
  const RECEIPTS = new RegExp(
    '^(?:' + RECEIPT_WORDS + '|message sent|enter to send)$' +
      // The relative form needs a DURATION after the word, not merely an "ago" somewhere: without
      // that, "seen it going for $30 not long ago" is a buyer's message we would have deleted.
      '|^(?:' + RECEIPT_WORDS + ')\\s+\\d+\\s*[a-z]{0,7}\\s+ago$',
    'i'
  );
  const NOTICES = new RegExp(
    '(started this chat|waiting for your response|send a quick response' +
      '|tap a response to send|you can now rate each other|people may rate one another' +
      '|you sent an attachment|view buyer profile)',
    'i'
  );
  // Built from strings rather than regex literals so every backslash is doubled: inside a JS
  // string '\d' collapses to a bare 'd', which silently breaks the pattern.
  const DAY = '(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*';
  const MONTH = '(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \\d{1,2}(?:, \\d{4})?';
  const TIMESTAMP = new RegExp(
    '^(?:' + DAY + '|' + MONTH + '),? \\d{1,2}:\\d{2}\\s?(?:am|pm)?$', 'i'
  );
  return RECEIPTS.test(line) || NOTICES.test(line) || TIMESTAMP.test(line);
}"""
_CONVERSATION_TAIL_TEMPLATE = """async () => {
  const isChromeLine = __IS_CHROME__;
  // Text the seller could click is never text the buyer sent. Facebook renders quick-reply
  // suggestions inside the message log — "Yes, are you interested?", "Sorry, it's not available."
  // — indistinguishable from a real message by text, position or shape; read as buyer messages,
  // the agent negotiates against words nobody said. The same rule drops the other clickable
  // furniture: a "Rate <buyer>" prompt, and link-preview cards.
  const clickable = (el, root) => {
    for (let n = el, i = 0; n && n !== root && i < 8; n = n.parentElement, i++) {
      if (n.getAttribute('role') === 'button') return true;
      if (getComputedStyle(n).cursor === 'pointer') return true;
    }
    return false;
  };
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
      if (isChromeLine(text)) return;
      if (counterpart && text === counterpart) return;
      if (title && text.indexOf(title) === 0) return;
      if (clickable(el, log)) return;
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

CONVERSATION_TAIL_JS = _CONVERSATION_TAIL_TEMPLATE.replace("__IS_CHROME__", CHROME_LINE_JS)

# Is the seller logged in? Three-state, and it must never answer logged_out on thin evidence: a
# false logged_out tells a signed-in seller to re-authenticate and stops their market. Only the
# password field proves logged_out; only a signed-in-only control (the chat rail) proves logged_in —
# a logged-out visitor gets the marketplace nav too, so that is deliberately not the marker.
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
# `/marketplace/you/selling` cards carry no listing id anywhere, so nothing read there can be
# joined to a conversation. The seller's public Marketplace profile carries the same listings as
# real `/marketplace/item/<id>` links, reached from the selling page by a link whose href holds
# the seller's account id — a fact about them, not about Facebook, so it is read from the page
# each time rather than stored.
#
# Answers `{url}`, or `{url: null}` when the link is not there, which the caller reports rather
# than reading the wrong page.
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
  // The seller's own listings, and nothing else. Their profile renders under a heading that
  // names them, and Facebook interleaves a "Today's picks" grid of OTHER people's listings down
  // the same page at overlapping positions. The first ancestor of the heading that holds any
  // listing link is the seller's grid and holds only it — read the wrong container and the survey
  // offers to relist strangers' items.
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
  // small inventory.
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
    // Title by position: a listing may legitimately be titled something that parses as a price.
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
    // Never report a partial grid as the whole inventory: short of the page's own tally, this
    // read did not finish.
    truncated: active > 0 && listings.length + dropped < active,
    visible: document.visibilityState === 'visible',
  };
}"""

MY_LISTINGS_JS = _MY_LISTINGS_TEMPLATE.replace(
    "__LISTING_ID_RE__", json.dumps(LISTING_ID_PATTERN)
).replace("__PARSE_PRICE__", jslib.PARSE_PRICE_JS)

# One listing's own page, read at adoption time.
#
# There is no JSON-LD for a marketplace item, so this is DOM work — and liveness is the dangerous
# field: `active` is true ONLY on a positive in-stock marker, because the cost of the other mistake
# is relisting something the seller already sold. The item's own block is located by the
# "Condition" label rather than position: an item URL renders the marketplace's whole chrome around
# a panel. Photographs are taken only from images Facebook labels "Product photo of …" — the page
# also carries a grid of similar listings from other sellers at the same CDN hosts.
LISTING_DETAIL_JS = """async () => {
  const parsePrice = __PARSE_PRICE__;
  // Facebook's own section labels inside the panel. Walking up stops at the first ancestor whose
  // opening line is NOT one of these — that ancestor's opening line is the title.
  const SECTIONS = ['Details', 'Condition', 'Description', 'Seller information'];
  // The panel's own footer, which sits where a description would be when there is not one.
  const TRAILER = /Location is approximate|^See (more|less)$|^Edit$|^Message$/i;
  const read = () => {
    // Anchored on "Condition" rather than the title, because the title cannot be recognised
    // without already knowing it: the biggest heading on an item page is the word "Marketplace".
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
    // Liveness, and the whole reason this fails closed: "In stock" is Facebook's own words on a
    // live listing, and anything we cannot read says nothing.
    const body = (panel.innerText || '');
    const live = /\\bIn stock\\b/i.test(body);
    const sold = /\\b(Sold|Out of stock|Pending|no longer available)\\b/i.test(body);
    return {
      active: live && !sold,
      title: title,
      // The line under the condition value, when there is one; a description-less listing runs
      // straight on to the footer, which is named and refused rather than adopted as the
      // seller's own words.
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

# --- publishing ----------------------------------------------------------------------------------

# The attribute the publish artifacts stamp on each control, and the selector built from it. The
# create form's fields carry no label, no name and no stable id — a field is identifiable only by
# the text next to it, which CSS cannot express. The artifact finds and marks them; the driver
# acts on the marks.
PUBLISH_MARK_ATTR = "data-sellee-publish"


def publish_target(step: str) -> str:
    """The selector for one marked publish control."""
    return f"[{PUBLISH_MARK_ATTR}='{step}']"


# Mark every control the publish driver needs, and say which were found.
#
# Answers `{marked: [...], missing: [...], boost_on, width, visible}`. The driver refuses to type
# anything until the fields it must fill are all present: the two text inputs are
# indistinguishable except by the label beside them, so a partly-recognised form could put the
# price in the title.
PUBLISH_FIELDS_JS = f"""() => {{
  const MARK = '{PUBLISH_MARK_ATTR}';
  // The floating label Facebook renders around a field: the nearest short text, which on a stack
  // of labelled boxes is this field's own label and not the section heading above it.
  const labelOf = (el) => {{
    for (let n = el, i = 0; n && i < 6; n = n.parentElement, i++) {{
      const t = (n.innerText || '').trim();
      if (t && t.length < 60) return t.split('\\n')[0].trim();
    }}
    return '';
  }};
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }};
  const mark = (el, step) => {{ if (el) el.setAttribute(MARK, step); return !!el; }};
  const byLabel = (selector, wanted) =>
    Array.from(document.querySelectorAll(selector)).filter(visible)
      .find((el) => labelOf(el) === wanted) || null;
  const button = (re) =>
    Array.from(document.querySelectorAll('[role="button"],button')).filter(visible)
      .find((el) => re.test((el.getAttribute('aria-label') || el.innerText || '').trim()))
    || null;

  const found = {{
    title: mark(byLabel('input[type="text"]', 'Title'), 'title'),
    price: mark(byLabel('input[type="text"]', 'Price'), 'price'),
    category: mark(byLabel('label[role="combobox"]', 'Category'), 'category'),
    condition: mark(byLabel('label[role="combobox"]', 'Condition'), 'condition'),
    description: mark(byLabel('textarea', 'Description'), 'description'),
    photos: mark(document.querySelector('input[type="file"]'), 'photos'),
    // The control that opens the file chooser, marked separately from the input: the upload only
    // works while a chooser is open, so the driver has to press this first.
    add_photos: mark(button(/^Add photos/), 'add_photos'),
    more: mark(button(/^More details/), 'more'),
    next: mark(button(/^Next$/), 'next'),
    publish: mark(button(/^Publish$/), 'publish'),
  }};
  // Paid promotion: a switch that ships default-off but is one stray click from not being, and
  // it spends the seller's money.
  const boost = Array.from(document.querySelectorAll('input[type="checkbox"]'))
    .find((el) => /Boost listing/i.test(el.getAttribute('aria-label') || ''));
  mark(boost, 'boost');
  // Whether the form will accept what it has. Facebook greys Next out until every required field
  // is filled, and clicking a disabled button submits nothing — the difference between "the form
  // is not ready" (try again) and "the publish may have gone through" (never again).
  const enabled = (step) => {{
    const el = document.querySelector('[' + MARK + "='" + step + "']");
    return !!el && el.getAttribute('aria-disabled') !== 'true';
  }};
  return {{
    marked: Object.keys(found).filter((k) => found[k]),
    missing: Object.keys(found).filter((k) => !found[k]),
    next_enabled: enabled('next'),
    publish_enabled: enabled('publish'),
    boost_on: !!(boost && boost.checked),
    width: window.innerWidth,
    visible: document.visibilityState === 'visible',
  }};
}}"""

# What one field holds now, for the read-back before publishing: a form that silently truncated a
# title, or dropped a price, must not become a live listing nobody checked.
PUBLISH_READBACK_JS = f"""() => {{
  const value = (step) => {{
    const el = document.querySelector("[{PUBLISH_MARK_ATTR}='" + step + "']");
    if (!el) return null;
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el.value;
    // A chosen dropdown renders as its own label above the value ("Category\\nFurniture"), so the
    // value is the last line.
    const lines = (el.innerText || '').trim().split('\\n').map((s) => s.trim()).filter(Boolean);
    return lines.length ? lines[lines.length - 1] : '';
  }};
  return {{
    title: value('title'),
    price: value('price'),
    description: value('description'),
    condition: value('condition'),
    category: value('category'),
  }};
}}"""

# The options of an open dropdown, marked so one can be clicked.
#
# The two dropdowns are not the same widget. Condition opens a `role="option"` list; Category
# opens a panel of `role="button"` rows — also what the form's own buttons are — so the menu is
# found structurally, as the largest column of short-text clickable rows. Same shape as
# Carousell's "the pane holding the most bubbles", for the same reason: it survives a layout
# nobody told us about.
_PUBLISH_OPTIONS_TEMPLATE = f"""() => {{
  const MARK = '{PUBLISH_MARK_ATTR}';
  const MIN_MENU = 4;
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    return r.width > 40 && r.height > 12;
  }};
  // A row's own label; Facebook hangs a subtitle under some rows, and it is not part of the name.
  const label = (el) => ((el.innerText || '').trim().split('\\n')[0] || '').trim();
  const rows = Array.from(document.querySelectorAll('[role="option"],[role="menuitem"]'))
    .filter(visible);
  let options = rows;
  if (!options.length) {{
    // A menu is a column: its rows share a left edge, where the form's own buttons are scattered
    // across the page. Grouping by parent does not work — each row is wrapped in its own div.
    const columns = new Map();
    Array.from(document.querySelectorAll('[role="button"]')).filter(visible).forEach((el) => {{
      const text = label(el);
      if (!text || text.length > 40) return;
      const left = Math.round(el.getBoundingClientRect().left);
      if (!columns.has(left)) columns.set(left, []);
      columns.get(left).push(el);
    }});
    let best = [];
    columns.forEach((members) => {{ if (members.length > best.length) best = members; }});
    options = best.length >= MIN_MENU ? best : [];
    options.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  }}
  const texts = options.map(label);
  const want = String(__WANTED__ || '').trim().toLowerCase();
  // Exact first, then a prefix — a listing must never be filed under a category that merely
  // contains the word we were looking for.
  let at = texts.findIndex((t) => t.toLowerCase() === want);
  if (at < 0) at = texts.findIndex((t) => t.toLowerCase().startsWith(want));
  if (at < 0) return {{ chosen: null, options: texts.slice(0, 40) }};
  options[at].setAttribute(MARK, 'option');
  return {{ chosen: texts[at], options: texts.slice(0, 40) }};
}}"""


def options_js(wanted: str) -> str:
    """The option-picking artifact, with the wanted text baked in.

    `browser_evaluate` passes one argument and it is the located element, so a value we choose is
    substituted here as a JS literal — the same way Carousell injects its listing-id pattern.
    """
    return _PUBLISH_OPTIONS_TEMPLATE.replace("__WANTED__", json.dumps(str(wanted or "")))


# Where the listing ended up, read after the publish settles. A publish that cannot be shown to
# have produced a listing is reported as unverified rather than done — the send bracket's rule,
# for the same reason: nobody can tell from the outside.
PUBLISH_RESULT_JS = """() => {
  const link = document.querySelector('a[href*="/marketplace/item/"]');
  const id = link
    ? ((link.getAttribute('href') || '').match(/\\/marketplace\\/item\\/(\\d+)/) || [])[1]
    : null;
  return {
    listing_id: id || null,
    url: location.href,
    text: ((document.body && document.body.innerText) || '').slice(0, 400),
  };
}"""

# Facebook's own condition wording, offered verbatim by the dropdown. Anything else is mapped by
# `condition_for`; an item with no usable condition does not publish, because guessing "New" for a
# used thing is a lie told to a buyer.
CONDITIONS = ("New", "Used - Like New", "Used - Good", "Used - Fair")


def condition_for(said: str) -> str:
    """Facebook's own word for an item's condition.

    Conditions are free text on an item and Facebook offers four; where they do not meet, this
    understates rather than overstates — calling something more used than it is costs the seller
    a little, and the reverse is a lie told on their behalf.
    """
    said = (said or "").strip().lower()
    if "like new" in said or "open box" in said:
        return "Used - Like New"
    if said.startswith("new") or said == "brand new":
        return "New"
    if "fair" in said or "heavily" in said or "well used" in said or "poor" in said:
        return "Used - Fair"
    return "Used - Good"


# Where a driven listing is filed when nothing has chosen better. Picking the right category from
# a title is judgement that belongs to the listing flow, not a driver; this is Facebook's own
# catch-all word, and one of the menu's options.
DEFAULT_CATEGORY = "Miscellaneous"


class FacebookPublish(publishing.PublishSurface):
    """Facebook's create form. The shared step defaults ARE this form's flow — it was the first
    driven market — so only the artifacts and the vocabulary are its own."""

    market = "fb"
    fields_js = PUBLISH_FIELDS_JS
    readback_js = PUBLISH_READBACK_JS
    result_js = PUBLISH_RESULT_JS
    default_category = DEFAULT_CATEGORY

    def target(self, step: str) -> str:
        return publish_target(step)

    def options_js(self, wanted: str) -> str:
        return options_js(wanted)

    def map_condition(self, said: str) -> str:
        return condition_for(said)


# Stateless, so one instance serves every drive.
PUBLISH_SURFACE = FacebookPublish()


# The reply composer, as shipped defaults under the heal cache.
#
# `page_url_pattern` is the conversation URL, not the marketplace inbox: a send happens on
# `/messages/t/<id>/`.
#
# There IS a send control here, and it is deliberately not used: the button at the end of the
# composer row sits in a composer the page repaints as it types, so a click resolves the element
# and then times out waiting for it to hold still. Its own label says what to do instead — with
# the composer focused from the typing, a real Enter is the send, on machinery that already works.
COMPOSER_DEFAULTS = (
    {
        "step": "message_box",
        "strategy": "css",
        "query": '[role="textbox"][contenteditable="true"]',
        "action_kind": "type",
        "page_url_pattern": "/messages/t/",
    },
)
