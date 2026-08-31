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
