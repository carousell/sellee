"""Gmail's webmail, read through a view scoped to Craigslist's mail.

The seller signs into their own webmail in the agent's Chrome, so **no credential is granted to us**
— no token, no app password, no API scope, nothing to leak. The scope limit is the *view*: the
transport navigates a search (or a label the seller made) that contains only mail this transport is
allowed to read.

**And that limit holds only in a tab that has shown nothing else. Measured, not assumed.** Gmail
keeps rendered list rows in the DOM across hash navigations, so a tab that has displayed `#inbox`
still holds those rows after navigating to the scoped search. On 2026-09-10, in one tab:

    first load of the scoped search      -> blocked: 0    (clean)
    after visiting #inbox and returning  -> blocked: 34   (the seller's own mail, in the DOM)
    a fresh tab, straight to the search  -> blocked: 0    (clean again)

34 of the seller's personal emails were sitting in the DOM of a correctly-scoped search — the hash
was right and the title said "Search results". So **every mail read must navigate a fresh tab**;
reusing one silently converts the structural guarantee into a hope. The same cache is why one
conversation appeared as two rows, which is why the list read de-duplicates, and why an empty result
is decided from Gmail's own words *before* the rows are looked at.

`mail/relay.py`'s sender check is therefore not a belt-and-braces second layer — it is what actually
held when the structural claim failed. It ran on all 34 and let none of their subjects out of the
page.

Captured live against a real signed-in mailbox holding a real buyer conversation:

  * **the scoped view is reachable by URL, and hash navigation re-renders** —
    `location.href = '…#search/…'` moved the page from `#inbox` to the search and changed the title
    to "Search results" (2026-09-09). That was an open question; it is answered.
  * a row is `tr.zA`, and the sender's address is the `email` attribute of a descendant.
  * an empty result is **stated in words** inside `[role="main"]`: "No messages matched your
    search." — which is what makes an empty mailbox distinguishable from an unreadable one.
  * **opening a conversation does not change the URL** (2026-09-10). The hash stayed
    `#search/from%3A(…)` with the thread open, so there is no thread id to read from `location`, and
    a thread cannot be re-opened by navigating to it. Identity has to come out of the DOM.
  * **the row's `id` is ephemeral** — the open thread's row was `id=":48"`, a Gmail widget id that
    is reassigned per render. An earlier version of this file returned it as `provider_message_id`.
    Keyed on that, `mail_relay_seen` matches nothing on a second read and **every buyer message is
    folded in again on every tick**: `reconcile` finds a new tail row each time, so the buyer looks
    like they said the same thing repeatedly and the agent answers it repeatedly.
  * the stable ids are Gmail's legacy hexes, and **both are reachable**:
    `[data-legacy-thread-id]` inside the list row (per conversation) and
    `.adn[data-legacy-message-id]` in an opened one (per message). Measured on a single-message
    thread the two were the *same hex* (`1a086d4fc4e321f6`), so neither may be derived from the
    other — each is read from its own attribute.
  * a list row carries **only a thread id**. Per-message ids exist only once a conversation is
    open, which is why the list read does not pretend to return one.
  * the reply control is `button[aria-label="Reply"]` — and `"Reply all"` and `"Forward"` sit beside
    it. See `REPLY_CONTROL_JS`: a substring match on "Reply" hits "Reply all", and reply-all on a
    relay thread copies every address craigslist put on it.
  * `.adL`/`.ajR` (Gmail's trimmed-quote toggle) was **present on a message with nothing trimmed**,
    so its presence is not evidence of hidden content and nothing here gates on it.

**On the JavaScript in this file.** The artifacts are templates with `__PLACEHOLDER__` substitution
rather than f-strings. Every one of them is full of literal `{` and an f-string needs each doubled,
which is a silent trap: a miscount produces `{{` in the page and a `SyntaxError` that surfaces as a
mailbox reporting itself permanently unreadable. One such miscount happened while writing the
handoff clause below. Templates have no brace rules.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from sellee.mail import relay

_SEARCH_BASE = "https://mail.google.com/mail/u/0/#search/"
_RELAY_CLAUSE = f"from%3A({relay.POSTING_DOMAIN}+OR+{relay.CONVERSATION_DOMAIN})"

# Craigslist's own footer phrase, quoted as an exact-phrase search. This is what keeps a forwarded
# thread in scope without widening to an address: only mail quoting a craigslist posting matches.
# `relay.FOOTER_MARKER` is the same string the body parser cuts on, so the view and the parser
# cannot drift.
FOOTER_PHRASE_QUERY = f'"{relay.FOOTER_MARKER}"'

# The relay-only view: the narrowest possible scope, and what `scoped_view` falls back to.
SEARCH_VIEW = f"{_SEARCH_BASE}{_RELAY_CLAUSE}"

# Gmail's stable, per-conversation and per-message ids. Named as attributes rather than inlined
# because the whole correctness of `mail_relay_seen` rests on reading these and not the widget id
# sitting next to them.
THREAD_ID_ATTR = "data-legacy-thread-id"
MESSAGE_ID_ATTR = "data-legacy-message-id"

# The reply control's exact accessible name. Exact, because "Reply all" is a *prefix* match away and
# sits immediately beside it in the same toolbar.
REPLY_LABEL = "Reply"
FORBIDDEN_REPLY_LABELS = ("Reply all", "Reply to all", "Forward")


# Where a seller is sent to sign in to the mailbox. Signed out, Gmail redirects this to
# accounts.google.com by itself, which is what makes one URL serve both states.
SIGN_IN_URL = "https://mail.google.com/mail/u/0/"

# The provider this transport can drive. Named because the connect refuses anything else with a
# reason rather than reading a webmail it has never been measured against — a wrong read of a
# mailbox is not a cosmetic failure.
PROVIDER = "gmail"
MAIL_HOST = "mail.google.com"
ACCOUNTS_HOST = "accounts.google.com"


# Is the seller signed in to the mailbox? Three states, fail-closed, and the closed direction is
# **not** `logged_out`.
#
# Saying `logged_out` to a signed-in seller tells them to re-authenticate a working mailbox and
# stops their Craigslist buyers being answered until they do — the same trap `craigslist.LOGIN_JS`
# is shaped around. So `logged_out` is claimed only from Google's own sign-in form on Google's own
# accounts host, `logged_in` only from a positive marker of the mail UI, and anything else abstains.
#
# It also reports the **provider**, because the connect refuses a non-Gmail mailbox rather than
# reading it wrongly. A seller who signs into Outlook in that window gets told why, not a mailbox
# that reports itself permanently unreadable.
LOGIN_JS = (
    """() => {
  const host = String(location.hostname || '').toLowerCase();
  const known = (name) => host === name || host.endsWith('.' + name);

  // Google's sign-in form, on Google's own accounts host. Both conditions: a password box alone
  // could be a re-auth panel inside a signed-in session.
  const onAccounts = known('__ACCOUNTS_HOST__');
  const credentialBox = !!document.querySelector(
    'input[type="password"], input[type="email"], #identifierId'
  );
  if (onAccounts && credentialBox) {
    return { state: 'logged_out', provider: '__PROVIDER__', host: host };
  }

  if (known('__MAIL_HOST__')) {
    // Positive markers of the mail UI, any one of which means a rendered mailbox. `[role=main]`
    // alone is too weak — the sign-in interstitial has one too.
    const mailUi = !!document.querySelector(
      '[gh="cm"], [gh="tm"], [gh="mtb"], div[role="navigation"] a[href*="#inbox"]'
    );
    if (mailUi) {
      return { state: 'logged_in', provider: '__PROVIDER__', host: host };
    }
    // On the mail host with nothing rendered: still loading, or a shape we have not seen. Not a
    // claim either way.
    return { state: 'unknown', provider: '__PROVIDER__', host: host, mail_ui: false };
  }

  // Some other webmail, or somewhere else entirely. The provider is reported as unknown so the
  // connect can refuse it by name instead of guessing at its DOM.
  return { state: 'unknown', provider: 'unknown', host: host };
}""".replace("__ACCOUNTS_HOST__", ACCOUNTS_HOST)
    .replace("__MAIL_HOST__", MAIL_HOST)
    .replace("__PROVIDER__", PROVIDER)
)


# The signed-in account's own address — what the handoff address is derived from.
#
# Nothing asks the seller to type it: the mailbox that receives a marketplace's relay mail *is* the
# address on that marketplace account, so the handoff is this address plus a tag. Read rather than
# configured means one less thing to mistype, and a `+tag` on the wrong domain would silently
# receive nothing.
#
# Two sources, captured live 2026-09-10, and the order matters:
#
#   a[aria-label]  "Google Account: Jerry Neo \n(jerry.neo@example.com)"   <- preferred, semantic
#   document.title "Inbox (361) - jerry.neo@example.com - Carousell Mail"  <- fallback
#
# The aria-label is preferred because the title's shape is a display string — it carries an unread
# count, changes with the view, and is localised. Both were present in the inbox and in a search.
#
# **Never returns an address it merely found on the page.** A mailbox full of craigslist relay mail
# has `<hex>@reply.craigslist.org` in dozens of places, and deriving a handoff from one of those
# would hand buyers an address that expires — the exact failure the handoff exists to prevent. So
# the address is taken only from the two account-identifying places above.
ACCOUNT_ADDRESS_JS = """() => {
  const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}/;

  const labelled = Array.from(document.querySelectorAll('[aria-label]'))
    .map((el) => el.getAttribute('aria-label') || '')
    .filter((said) => /google account/i.test(said) && EMAIL.test(said));
  if (labelled.length) {
    const found = labelled[0].match(EMAIL);
    if (found) {
      return { address: found[0].toLowerCase(), source: 'account_label' };
    }
  }

  // The title's middle segment, which is where the address sits in every view measured.
  const parts = String(document.title || '').split(' - ');
  for (let i = 0; i < parts.length; i += 1) {
    const found = parts[i].trim().match(EMAIL);
    // Anchored to a whole segment so a subject line containing an address cannot be mistaken for
    // the account's own.
    if (found && found[0] === parts[i].trim()) {
      return { address: found[0].toLowerCase(), source: 'title' };
    }
  }

  return {
    error: 'could not read which account this mailbox belongs to',
    titled: !!document.title,
  };
}"""


def search_view(handoff: str = "") -> str:
    """The scoped search: craigslist's relay senders, plus anything quoting a craigslist posting.

    **Why a second clause at all.** A relay address expires and cannot be recovered (see `relay.py`
    — measured), so a reply invites the buyer onto a durable address of the seller's own. The moment
    they use it they stop being a craigslist sender and become `somebuyer@gmail.com`, which a
    relay-only view does not list: the transport would be blind to the very conversation the
    invitation created.

    **Why the clause is content and not an address.** An earlier version scoped on
    `deliveredto:<seller+cl@…>`, so the widening was to one address existing only for craigslist.
    That required the seller's provider to route subaddressing, and where it does not the
    invitation names an address nobody receives — reported by a seller for whom `+cl` was not real.
    A forwarded craigslist thread carries craigslist's own footer instead, and the phrase is what
    this searches for.

    **Measured on a live mailbox**: `"Original craigslist post"` matched exactly four messages —
    two buyer messages and the two replies to them — and nothing else in the mailbox. So the scope
    is narrow without asking anything of the provider. It also brings the agent's *own* replies into
    view, which is what lets a later read settle a send whose confirmation was missed.

    `handoff` is accepted and unused: the scope no longer depends on it, and the parameter stays so
    the caller does not have to know that.
    """
    return f"{_SEARCH_BASE}{_RELAY_CLAUSE}+OR+{quote(FOOTER_PHRASE_QUERY, safe='')}"


def scoped_view(configured: str = "", handoff: str = "") -> str:
    """The view to navigate: whatever the seller configured, else the craigslist search.

    A configured value is preferred without inspection beyond its scheme (the setting's parser has
    already refused a non-https one) because it is the seller's own label — the point of letting
    them set it is that they decide what we can see. A seller who has made a label is responsible
    for it covering the handoff address too, which is why the handoff is only appended to the
    search we build ourselves.
    """
    return configured.strip() or search_view(handoff)


# The one definition of "mail this transport may read", as a JavaScript predicate named `inScope`.
#
# Two clauses. The first is craigslist's relay domains. The second is the seller's craigslist
# handoff subaddress (see `search_view`), and it is a whole-address match against a single value —
# never a domain, because the handoff lives on the seller's own mail domain and matching that domain
# would admit their entire mailbox.
#
# Both clauses match on a boundary rather than as a substring: `sale.craigslist.org.evil.com` is not
# the relay, and `seller+cl@example.com.evil.com` is not the handoff.
_SCOPE_GUARD = """
  const RELAY = __RELAY__;
  const HANDOFF = __HANDOFF__;
  const FOOTER = __FOOTER__;
  const bare = (address) => String(address || '').trim().toLowerCase();
  const domainOf = (address) => bare(address).split('@').pop();
  // By sender: craigslist's relay, or the seller's own handoff address (which is what their own
  // sent replies come from).
  const inScope = (address) => (
    RELAY.indexOf(domainOf(address)) !== -1 || HANDOFF.indexOf(bare(address)) !== -1
  );
  // By content: a forwarded craigslist thread. A buyer who forwards writes from their own ordinary
  // address, so no sender test can admit them — but the forward carries craigslist's footer and the
  // posting permalink. Both required: the phrase alone admits a message that merely mentions
  // craigslist, the URL alone admits a pasted link.
  const quotesAPosting = (body) => {
    const said = String(body || '');
    if (said.indexOf(FOOTER) === -1) { return false; }
    const POSTING = new RegExp(
      'https://(?:[a-z0-9-]+\\\\.)?craigslist\\\\.org/(?:view/)?[A-Za-z0-9/_-]*d/', 'i'
    );
    return POSTING.test(said);
  };
"""


def _guard(handoff: str = "") -> str:
    said = str(handoff or "").strip().lower()
    return (
        _SCOPE_GUARD.replace("__RELAY__", json.dumps(list(relay.RELAY_DOMAINS)))
        .replace("__HANDOFF__", json.dumps([said] if said else []))
        .replace("__FOOTER__", json.dumps(relay.FOOTER_MARKER))
    )


def _build(template: str, handoff: str = "") -> str:
    return (
        template.replace("__SCOPE_GUARD__", _guard(handoff))
        .replace("__THREAD_ID_ATTR__", THREAD_ID_ATTR)
        .replace("__MESSAGE_ID_ATTR__", MESSAGE_ID_ATTR)
    )


# The scoped view's rows: who wrote, what about, and the *stable* id of the conversation.
#
# Three answers, and only the last is retried:
#
#   Gmail says none matched  -> an honestly empty view, whatever rows are lying around
#   rows found               -> the conversations
#   neither                  -> `{error}`: not loaded, signed out, or a shape we have not seen
#
# **That order is load-bearing, and it was wrong.** This checked `rows.length` first. Measured live
# 2026-09-10, a search for an unused subaddress rendered "No messages matched your search." *and*
# left 35 rows from the previous view in the DOM — Gmail's row cache again. Rows-first, the artifact
# hands those leftovers back as live conversations of the current view: stale threads folded in, and
# a conversation resurrected after it was already answered. Gmail's own words about the current
# query beat the DOM's residue from the last one, so they are consulted first, unconditionally.
#
# `stale_rows` reports the residue rather than hiding it, because a non-zero count on an empty view
# is the signature of a reused tab — which is the condition the whole scope guarantee depends on not
# being in.
_MESSAGE_LIST_TEMPLATE = """() => {
  __SCOPE_GUARD__
  const main = document.querySelector('[role="main"]');
  const said = main ? (main.innerText || '') : '';
  const rows = Array.from(document.querySelectorAll('tr.zA'));

  // Gmail's own words for an empty result, consulted before the rows because stale rows outlive a
  // view change. A fact about the mailbox, not a failure to read it — reported as an error this
  // would climb a blind counter and tell the seller to check a mailbox that is simply empty.
  if (/no messages matched/i.test(said)) {
    return { conversations: [], empty_stated: true, stale_rows: rows.length };
  }

  if (!rows.length) {
    return {
      error: 'the mail view showed neither messages nor "no messages matched"',
      has_main: !!main,
      chars: said.length,
      visible: document.visibilityState === 'visible',
    };
  }

  const conversations = [];
  const already = {};
  let blocked = 0;
  let unidentified = 0;
  let duplicates = 0;
  rows.forEach((row) => {
    const holder = row.querySelector('[email]');
    const address = holder ? holder.getAttribute('email') : '';
    // Sender-verified, or not. A row this guard cannot vouch for by sender is **not** dropped —
    // that is how a forwarded craigslist thread arrives, from the buyer's own ordinary address —
    // but nothing about it leaves the page except its opaque id. Its subject stays here, and the
    // tail read decides by content whether it is craigslist's at all.
    const verified = inScope(address);
    // The stable conversation id, never `row.id` — that is a widget id, reassigned per render.
    const keyed = row.querySelector('[__THREAD_ID_ATTR__]');
    const threadId = keyed ? (keyed.getAttribute('__THREAD_ID_ATTR__') || '') : '';
    // No stable id means nothing can tell this conversation apart on the next read, so folding it
    // in would re-journal it forever. Counted and left behind instead.
    if (!threadId) { unidentified += 1; return; }
    // Gmail's row cache can hold two rows for one conversation — the same thread as rendered by an
    // earlier view and by this one (measured). Returned twice, the conversation is folded twice in
    // one tick and the buyer is answered twice.
    if (already[threadId]) { duplicates += 1; return; }
    already[threadId] = true;
    if (!verified) {
      blocked += 1;
      conversations.push({
        provider_thread_id: threadId,
        verified: false,
        unread: row.className.indexOf('zE') !== -1,
      });
      return;
    }
    const subject = row.querySelector('.bog');
    conversations.push({
      provider_thread_id: threadId,
      verified: true,
      sender: String(address || '').toLowerCase(),
      subject: subject ? (subject.innerText || '').trim() : '',
      unread: row.className.indexOf('zE') !== -1,
    });
  });

  return {
    conversations: conversations,
    // How many rows this guard could not vouch for by sender. Some are forwarded craigslist
    // threads (confirmed by content when opened) and some are the seller's other mail; either way
    // nothing but an opaque id left the page for them.
    blocked: blocked,
    unidentified: unidentified,
    duplicates: duplicates,
  };
}"""


def message_list_js(handoff: str = "") -> str:
    """The list read, scoped to relay mail plus the handoff address."""
    return _build(_MESSAGE_LIST_TEMPLATE, handoff)


# An opened conversation's messages, in the order Gmail renders them.
#
# Answers `{error}` rather than an empty tail whenever the read is not trustworthy: an empty tail is
# indistinguishable from "the buyer said nothing", and acting on it would have the agent answer a
# conversation it cannot see. The thread's own id is returned so the caller can check it is reading
# the conversation it asked for — the URL cannot tell it, because opening a thread does not change
# the URL.
#
# The body is returned raw. Craigslist's footer is stripped in Python by `relay.buyer_text`, where
# it is pure and testable, rather than in a string that only runs inside a browser.
_CONVERSATION_TAIL_TEMPLATE = """() => {
  __SCOPE_GUARD__
  const header = document.querySelector('h2.hP[__THREAD_ID_ATTR__], [__THREAD_ID_ATTR__]');
  const openedId = header ? (header.getAttribute('__THREAD_ID_ATTR__') || '') : '';
  const holders = Array.from(document.querySelectorAll('.adn'));

  if (!holders.length) {
    return {
      error: 'no message bodies in the opened conversation',
      opened_thread_id: openedId,
      visible: document.visibilityState === 'visible',
    };
  }

  const messages = [];
  let blocked = 0;
  let unidentified = 0;
  holders.forEach((holder) => {
    const who = holder.querySelector('span[email]');
    const address = who ? (who.getAttribute('email') || '') : '';
    const rawBody = (holder.querySelector('.a3s') || {}).innerText || '';
    // Admitted by sender, or by content. The content clause is what lets a *forwarded* thread be
    // read: the buyer writes from their own address, and the forward carries craigslist's footer
    // and the posting permalink. Anything satisfying neither does not leave the page as a body.
    if (!inScope(address) && !quotesAPosting(rawBody)) { blocked += 1; return; }
    const messageId = holder.getAttribute('__MESSAGE_ID_ATTR__') || '';
    if (!messageId) { unidentified += 1; return; }
    messages.push({
      provider_message_id: messageId,
      sender: String(address || '').toLowerCase(),
      // Craigslist rewrites the address and **not** the display name (measured), so a buyer's real
      // name arrives here. Kept because it is useful — a reply that can say "hi Sam" reads like a
      // person — and because normal disclosure in both directions is how a craigslist sale works.
      sender_name: who ? (who.getAttribute('name') || '').trim() : '',
      // Every address craigslist put on the message. What a reply-all would reach, which is why
      // the send path never uses one.
      recipients: Array.from(holder.querySelectorAll('span[email]'))
        .map((e) => String(e.getAttribute('email') || '').toLowerCase()),
      body: rawBody,
    });
  });

  if (!messages.length) {
    return {
      error: 'the opened conversation held no readable messages in scope',
      opened_thread_id: openedId,
      blocked: blocked,
      unidentified: unidentified,
    };
  }

  return {
    opened_thread_id: openedId,
    messages: messages,
    blocked: blocked,
    unidentified: unidentified,
  };
}"""


def conversation_tail_js(handoff: str = "") -> str:
    """The tail read, scoped to relay mail plus the handoff address."""
    return _build(_CONVERSATION_TAIL_TEMPLATE, handoff)


# Open one conversation, chosen by its stable id rather than by position.
#
# By id and not "the first row" or "the unread one", because the list this ran against is not
# necessarily the list the caller decided from: Gmail's row cache can hold rows from an earlier
# view, and a read that clicked by position could open a conversation the caller never chose — and
# then attribute a buyer's message to the wrong thread.
#
# Refuses on anything but exactly one match. Two rows for one id is Gmail's cache (measured), and
# clicking either is fine, but a caller that asked for an id and got none must hear so rather than
# read whatever happened to be open.
_OPEN_CONVERSATION_TEMPLATE = """() => {
  const WANT = __WANT__;
  const rows = Array.from(document.querySelectorAll('tr.zA')).filter((row) => {
    const keyed = row.querySelector('[__THREAD_ID_ATTR__]');
    return keyed && keyed.getAttribute('__THREAD_ID_ATTR__') === WANT;
  });
  if (!rows.length) {
    return { error: 'no row for that conversation in the current view', want: WANT };
  }
  // The subject cell, because clicking the row can land on the checkbox or a hover action.
  const target = rows[0].querySelector('.bog') || rows[0];
  target.click();
  return { opened: WANT, rows: rows.length };
}"""


def open_conversation_js(provider_thread_id: str) -> str:
    """Click the row for one conversation, by its stable id."""
    return _build(
        _OPEN_CONVERSATION_TEMPLATE.replace("__WANT__", json.dumps(str(provider_thread_id)))
    )


# Where a reply starts, and the control it must never be.
#
# Gmail puts "Reply", "Reply all" and "Forward" in one toolbar, and matches on accessible name. A
# substring test for "Reply" selects "Reply all" just as happily — and reply-all on a relay thread
# copies every address craigslist put on the message (measured: both the `sale.` posting address and
# the `reply.` conversation address are on it), turning a private answer into a broadcast and
# sending mail to craigslist's own posting endpoint.
#
# So the match is exact, the forbidden labels are checked by name, and the control is *reported*
# rather than clicked — the caller decides to act, and can see what it is about to act on.
REPLY_CONTROL_JS = """() => {
  const WANT = __WANT__;
  const FORBIDDEN = __FORBIDDEN__;
  const labelled = Array.from(document.querySelectorAll('[aria-label]'));

  const named = labelled
    .map((el) => ({ el: el, label: (el.getAttribute('aria-label') || '').trim() }))
    .filter((found) => found.label);

  const exact = named.filter((found) => found.label === WANT);
  if (!exact.length) {
    return {
      error: 'no control named exactly "' + WANT + '" in the opened conversation',
      // What was there instead, so a Gmail relabelling is diagnosable rather than a silent refusal.
      nearby: named
        .filter((found) => /repl|forward/i.test(found.label))
        .map((found) => found.label)
        .slice(0, 8),
    };
  }
  if (exact.length > 1) {
    return { error: 'more than one control named exactly "' + WANT + '"', count: exact.length };
  }

  const found = exact[0];
  if (FORBIDDEN.indexOf(found.label) !== -1) {
    return { error: 'refusing the control named "' + found.label + '"' };
  }
  return {
    label: found.label,
    tag: found.el.tagName.toLowerCase(),
    disabled: !!found.el.disabled,
  };
}""".replace("__WANT__", json.dumps(REPLY_LABEL)).replace(
    "__FORBIDDEN__", json.dumps(list(FORBIDDEN_REPLY_LABELS))
)


# The compose body, and the two things that are *not* it.
#
# Measured live 2026-09-10 with a reply open, `[aria-label="Message Body"]` matched **two**
# elements — a hidden `textarea.Ak` and the real `div[contenteditable="true"]` — so the label alone
# is ambiguous and typing into the wrong one sends an empty message. And a bare
# `div[contenteditable="true"]` is worse: `div[aria-label="Ask Gemini"][contenteditable="true"]` is
# also on the page, so the reply would be typed into Gemini's prompt box and never sent anywhere.
#
# Both conditions together are what identifies it.
COMPOSE_BODY_SELECTOR = 'div[aria-label="Message Body"][contenteditable="true"]'

# Gmail seeds an empty reply body with this (measured). Recorded rather than used: it is the reason
# `COMPOSE_FILL_JS` confirms the write by *comparing what it wrote* instead of testing the body for
# emptiness — an untouched body is not empty, it holds this, so an emptiness test would call a
# failed type a success. Nothing reads it; the note is the point.
COMPOSE_PLACEHOLDER = "Press / to write using your Gmail"

# The send control's exact accessible name, and its neighbours.
#
# "Send feedback to Google" precedes the real control in DOM order and contains "Send", so the
# first substring match on the page is a feedback dialog. "More send options" is schedule-send.
SEND_LABEL = "Send"
FORBIDDEN_SEND_LABELS = ("Send feedback to Google", "More send options", "Schedule send")


# Who the open reply is addressed to — the check that runs *before* anything is sent.
#
# The reason is delivery rather than secrecy: disclosing what a craigslist seller normally discloses
# is fine (see `outbound.py`). But if Gmail has addressed this reply anywhere other than the relay —
# a resolved contact, an autocompleted address, a Cc craigslist put on the original — it does not
# reach this buyer. Worse, a `sale.` address is where a *buyer* writes in order to reach the
# *seller*, so a reply sent there mails the seller their own answer while the buyer waits. And
# nothing downstream notices: a send to a retired relay address was measured producing no bounce at
# all. So the recipients are read and asserted rather than trusted.
#
# Measured 2026-09-10: an inline reply's recipients are `[data-hovercard-id]` chips inside
# `[name="to"]` (and `[name="cc"]` / `[name="bcc"]`), not `input` values — the fields are empty
# strings even when addressed. Reading `.value` here would find nothing and conclude, wrongly, that
# the reply was addressed to no one.
COMPOSE_RECIPIENTS_JS = """() => {
  const fields = ['to', 'cc', 'bcc'];
  const found = {};
  let any = false;
  fields.forEach((field) => {
    const holder = document.querySelector('[name="' + field + '"]');
    if (!holder) { found[field] = []; return; }
    any = true;
    found[field] = Array.from(holder.querySelectorAll('[data-hovercard-id]'))
      .map((chip) => String(chip.getAttribute('data-hovercard-id') || '').toLowerCase())
      .filter(Boolean);
  });
  if (!any) {
    return { error: 'no recipient fields on the page — is a reply actually open?' };
  }
  return { to: found.to, cc: found.cc, bcc: found.bcc };
}"""


# Put the reply in the body, and confirm it landed.
#
# **Line breaks have to be built, not written.** The first version assigned
# `body.textContent = text` and compared `innerText` back exactly. Measured live on a
# two-paragraph reply: the text landed
# (279 characters) but the check failed, because HTML collapses whitespace — a `\n` inside a
# contenteditable renders as a space, so the read-back did not match. The strict comparison caught
# it, and that is the only reason a buyer did not receive the whole reply as one run-on paragraph.
#
# So each line becomes its own `<div>`, which is Gmail's own structure, built with
# `createElement` + `textContent` per line so nothing in the reply can be interpreted as markup. An
# empty line becomes a `<br>`, which is how a blank line survives.
#
# The read-back check stays, because a contenteditable that silently ignored the write is otherwise
# indistinguishable from one that took it and the difference decides whether an empty message gets
# sent. It compares on **collapsed whitespace**: strict enough to catch a write that did not happen,
# not so strict that the browser's own rendering of a paragraph break reads as a failure.
_COMPOSE_FILL_TEMPLATE = r"""() => {
  const text = __TEXT__;
  const body = document.querySelector(__BODY_SELECTOR__);
  if (!body) {
    return { error: 'no reply body open' };
  }
  const lines = String(text == null ? '' : text).split('\n');

  body.focus();
  body.textContent = '';
  lines.forEach((line) => {
    const holder = document.createElement('div');
    if (line) {
      // textContent, never innerHTML: nothing in a reply may be interpreted as markup.
      holder.textContent = line;
    } else {
      holder.appendChild(document.createElement('br'));
    }
    body.appendChild(holder);
  });
  body.dispatchEvent(new Event('input', { bubbles: true }));

  // Collapsed-whitespace comparison. The browser decides how a paragraph break renders; what
  // matters is that every word we meant to send is present.
  const flatten = (said) => String(said || '').replace(/\s+/g, ' ').trim();
  const readBack = body.innerText || '';
  if (flatten(readBack) !== flatten(text)) {
    return {
      error: 'the reply body did not accept the text',
      chars: readBack.length,
      wanted_chars: String(text || '').length,
    };
  }
  return { filled: true, chars: readBack.length, lines: lines.length };
}""".replace("__BODY_SELECTOR__", json.dumps(COMPOSE_BODY_SELECTOR))


def compose_fill_js(text: str) -> str:
    """Fill the reply body with `text`.

    The text is baked into the artifact rather than passed as an argument because the browser
    server's evaluate takes a function and nothing else — the same reason every other parameterised
    artifact here is a factory. JSON-encoded, so a reply containing quotes or newlines cannot break
    out of the literal.
    """
    return _COMPOSE_FILL_TEMPLATE.replace("__TEXT__", json.dumps(str(text)))


# Open the reply. Separate from `REPLY_CONTROL_JS`, which *reports* the control without touching it:
# the caller looks first so it can refuse on a Gmail relabelling with something diagnosable, and
# only then acts. Both match the accessible name exactly, because "Reply all" is a prefix away and
# reply-all on a relay thread copies every address craigslist put on the message.
CLICK_REPLY_JS = """() => {
  const WANT = __WANT__;
  const FORBIDDEN = __FORBIDDEN__;
  const exact = Array.from(document.querySelectorAll('[aria-label]'))
    .map((el) => ({ el: el, label: (el.getAttribute('aria-label') || '').trim() }))
    .filter((found) => found.label === WANT && FORBIDDEN.indexOf(found.label) === -1);
  if (exact.length !== 1) {
    return { error: 'expected exactly one control named "' + WANT + '", found ' + exact.length };
  }
  exact[0].el.click();
  return { clicked: true };
}""".replace("__WANT__", json.dumps(REPLY_LABEL)).replace(
    "__FORBIDDEN__", json.dumps(list(FORBIDDEN_REPLY_LABELS))
)


# The send click, refusing every control that merely looks like it.
SEND_JS = """() => {
  const WANT = __WANT__;
  const FORBIDDEN = __FORBIDDEN__;
  const named = Array.from(document.querySelectorAll('[aria-label]'))
    .map((el) => ({ el: el, label: (el.getAttribute('aria-label') || '').trim() }))
    .filter((found) => found.label && FORBIDDEN.indexOf(found.label) === -1);
  const exact = named.filter((found) => found.label === WANT);
  if (exact.length !== 1) {
    return {
      error: 'expected exactly one control named "' + WANT + '", found ' + exact.length,
      nearby: named.filter((f) => /send/i.test(f.label)).map((f) => f.label).slice(0, 8),
    };
  }
  exact[0].el.click();
  return { clicked: true };
}""".replace("__WANT__", json.dumps(SEND_LABEL)).replace(
    "__FORBIDDEN__", json.dumps(list(FORBIDDEN_SEND_LABELS))
)


# Did the send happen? Three answers, and the difference between them is the difference between
# `SendUnverified` and a lie.
#
# Captured live 2026-09-10, clicking Send on a real reply and polling the toast:
#
#     t+0.6s   [role=alert]  "Sending... Cancel"   #link_undo present
#     t+~1s    [role=alert]  "Message sent"        #link_undo present
#     t+9.0s   [role=alert]  (gone)
#
# Three things that shape this:
#
#   * **"Sending..." is not "sent".** A verifier that fires on the first `[role="alert"]` reads
#     "Sending...", reports the buyer answered, and is wrong — the message is still in flight and
#     still cancellable. So the in-flight wording is tested *first* and answers `sending`, which
#     means poll again, not success and not failure.
#   * **`#link_undo` is present in both states**, so it is not a terminal signal and nothing here
#     uses it. It looked like the obvious one.
#   * **The evidence expires after about nine seconds.** Read later than that, a successful send and
#     a send that never happened look identical. That is `unknown`, which the caller must treat as
#     `SendUnverified`: handed over, unconfirmable, never re-driven. It cannot be resolved by
#     reading the thread later either, because the scoped view holds only craigslist's messages and
#     our own reply is not one of them (measured: the thread still showed one message afterwards).
#
# And `sent` here means *Gmail accepted it*, which is not delivery. A reply confirmed by this toast,
# present in Sent, was measured never reaching the buyer and producing no bounce (see `relay.py`).
SENDING_TOAST = "Sending"
SENT_TOAST = "Message sent"

SEND_VERIFY_JS = """() => {
  const alerts = Array.from(document.querySelectorAll('[role="alert"]'))
    .map((el) => (el.innerText || '').trim())
    .filter(Boolean);
  const said = alerts.join(' | ');

  // In flight first. "Sending..." carries a Cancel affordance, and reporting it as sent is the one
  // wrong answer that cannot be walked back.
  if (/__SENDING__/i.test(said) || /\\bcancel\\b/i.test(said)) {
    return { sending: true, said: said };
  }
  if (/__SENT__/i.test(said)) {
    return { sent: true, said: said };
  }
  // No evidence either way. The toast lasts about nine seconds; after that a send that worked and
  // one that never happened are indistinguishable from the page.
  return { unknown: true, said: said, alerts: alerts.length };
}""".replace("__SENDING__", SENDING_TOAST).replace("__SENT__", SENT_TOAST)
