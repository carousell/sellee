"""Craigslist's browser contract: a session probe, the seller's own postings, and one listing.

Craigslist is a narrower adapter than the others by design, because it differs three ways:

**It has no messaging.** Buyers reach a seller at an anonymised relay email
(`rcc<hex>@sale.craigslist.org`), and the account page shows no replies at all. So the
conversation artifacts here are not stubs awaiting work — they are the honest answer, and they say
so. Answering buyers is a mail transport's job, not this lane's.

**Its account surface is server-rendered on another host.** `www.craigslist.org/account` is an
`<iframe>` shell (`window.cl.init(..., 'framedApplication', ...)`) whose content is a different
origin, so `browser_evaluate` on the top document cannot read into it. The legacy
`accounts.craigslist.org/login/home` is the real page: server-rendered, and it 302s to a login
form when the session is gone, which is what makes a three-state probe possible at all. The
registry reaches it as an absolute URL.

**Its listings expire rather than sell.** There is no sold state — only delete, expire and flag —
and a free post dies in 7-45 days. So `active` here proves *present*, never *unsold*, and a caller
that needs "still for sale" has to ask the seller.

Selectors captured from live pages on 2026-09-09, not written from memory: the signed-out login
page at `accounts.craigslist.org/login/home`, and a live `sfo` posting reached through the
area-partitioned sitemap that `robots.txt` publishes.
"""

from __future__ import annotations

# Where a listing's id sits in its permalink, as one capture group (`reconcile.listing_id` takes
# group 1). Craigslist serves two shapes and both are live, so the group alternates:
#
#   canonical   /view/d/<slug>/m6hAC6XHtzngBDsBNy9GXm   -> a 22-char base62 token
#   legacy      /<sub>/<cat>/d/<slug>/7963311351.html   -> the numeric id; 301s to the above
#
# The numeric branch is first because a legacy URL's digits are unambiguous; a canonical token
# starts with a letter often enough that the branches do not collide, and an all-digit token is
# still a usable id. Whatever this captures, `MY_LISTINGS_JS` must capture the same thing from the
# same href — a mismatched id space means re-adoption silently makes a second item for one listing.
LISTING_ID_PATTERN = r"/(?:view/)?d/[^/]+/([0-9]+|[A-Za-z0-9]{18,26})(?:\.html)?/?$"


# Whether the seller's craigslist session is alive, read on `accounts.craigslist.org/login/home`.
#
# Three-state, and it never guesses `logged_out`: a false one tells a signed-in seller to
# re-authenticate and stops their market. Each state needs its own positive evidence, and anything
# else is `unknown` — which the caller answers by looking again rather than by acting.
#
#   logged_out  a password field inside the accounts login form. Captured live: the signed-out page
#               is titled "craigslist - account log in" and carries `input[type=password]`,
#               `#inputEmailHandle`, `#login` and `#onetime`. A signed-in account page has no
#               password field, so this cannot fire on one.
#   logged_in   a control only a session can see — a logout link, or the postings management the
#               page exists to show.
LOGIN_JS = """() => {
  const seen = (selector) => !!document.querySelector(selector);
  const text = ((document.body && document.body.innerText) || '').toLowerCase();

  // Logged out: the login form's own controls, not merely the word "log in" somewhere on a page.
  const passwordField = seen('input[type="password"]');
  const loginForm = !!Array.from(document.querySelectorAll('form')).find((form) =>
    /accounts\\.craigslist\\.org\\/login/.test(form.getAttribute('action') || '')
  );
  if (passwordField && (loginForm || seen('#inputEmailHandle'))) {
    return { state: 'logged_out' };
  }

  // Logged in: something only a session renders. A logout control is the strongest, and the
  // manage forms every postings row carries are the fallback.
  const logout = !!Array.from(document.querySelectorAll('a')).find((a) => {
    const href = a.getAttribute('href') || '';
    const label = (a.innerText || '').trim().toLowerCase();
    return /\\/logout/.test(href) || /^log ?out$/.test(label);
  });
  if (logout || seen('form.manage') || seen('input[name="crypt"]')) {
    return { state: 'logged_in' };
  }

  // Anything else abstains, and carries what it saw so a wrong answer is diagnosable rather than
  // just wrong. A blank page and a redesigned one must not be told apart by guessing.
  return {
    state: 'unknown',
    password_field: passwordField,
    login_form: loginForm,
    chars: text.length,
    visible: document.visibilityState === 'visible',
  };
}"""


# Craigslist has no in-page inbox, so this is the truth rather than a placeholder.
#
# An empty conversation list is a *healthy* read: it clears the read lane's blind counter, where an
# `{error}` would accumulate toward a needs-me notice telling the seller to go and look at a page
# that was never going to hold anything. The lane still navigates and still runs the login probe,
# which is the whole reason this market keeps an `inbox` URL at all — that probe is what notices a
# dropped session, and the survey backfill rides on it.
CONVERSATIONS_LIST_JS = """() => {
  // Not a stub: craigslist routes buyers to an anonymised relay email, and the account page shows
  // no replies. There is nothing here to list, and saying so is the honest answer.
  return { conversations: [], reason: 'craigslist has no on-site inbox' };
}"""


# Never called — the conversation list is always empty — and it abstains rather than claiming an
# empty conversation, because `null` means "could not read" and `[]` would mean "nobody said
# anything", which is a different and false claim.
CONVERSATION_TAIL_JS = """() => null"""


# One listing's own page: the fields an item needs, and whether the post is still up.
#
# Captured live 2026-09-09. Craigslist emits schema.org JSON-LD at a *stable id* —
# `script#ld_posting_data` — which is a contract with search engines rather than with us, and
# carries the title, description, price, currency and full-size photographs. Three things it does
# **not** carry, all read from the DOM instead: the condition, the post id, and any date.
#
# `active` proves the post is *present*, never that the item is unsold: craigslist has no sold
# state, and a deleted or expired post answers 410 with a `div.removed` banner and nothing else of
# the original. Fails closed — a page whose liveness cannot be read answers `null` (abstain) rather
# than `{active: false}`, because a falsy `active` is terminal in `adopt.py` and would drop the
# seller's listing for good on one slow load.
LISTING_DETAIL_JS = """() => {
  // Removed states first: craigslist serves 410 with this banner for both a deleted and an expired
  // post, and nothing of the original survives on the page.
  if (document.querySelector('#has_been_removed, .removed')) {
    return { active: false, availability: 'removed' };
  }

  const body = document.querySelector('#postingbody');
  const posting = document.querySelector('script#ld_posting_data');
  // Neither marker means we are not looking at a readable posting yet — abstain, never deny.
  if (!body && !posting) return null;

  let data = {};
  if (posting) {
    try { data = JSON.parse(posting.textContent || '{}') || {}; } catch (e) { data = {}; }
  }
  const offers = data.offers || {};

  // The description, minus the print/QR block craigslist injects inside the body — left in, every
  // adopted item's description opens with "QR Code Link to This Post".
  let description = '';
  if (body) {
    const clone = body.cloneNode(true);
    clone.querySelectorAll('.print-information, .print-qrcode-container')
      .forEach((el) => el.remove());
    description = (clone.innerText || '').trim();
  }

  // Condition is the *text* of the value span, which wraps an <a> behind whitespace — a
  // first-text-node read returns empty. The anchor's href also carries craigslist's numeric code
  // (condition=20 is "like new"), but the word is what an item records.
  const conditionEl = document.querySelector('.attr.condition .valu');
  const condition = conditionEl ? (conditionEl.innerText || '').trim() : '';

  // The post id is printed on the page and nowhere in the canonical URL, which carries a token.
  let postId = null;
  document.querySelectorAll('.postinginfos .postinginfo').forEach((el) => {
    const found = (el.innerText || '').match(/post id:\\s*(\\d+)/i);
    if (found) postId = found[1];
  });

  const posted = document.querySelector('.postinginfos time[datetime]');
  const price = offers.price != null ? Number(offers.price) : null;

  return {
    active: true,
    availability: 'live',
    title: (data.name || '').trim(),
    description: description,
    price: Number.isFinite(price) ? price : null,
    currency: offers.priceCurrency || null,
    condition: condition || null,
    photo_urls: (data.image || []).filter((url) => /^https:/.test(url)),
    listing_id: postId,
    posted_ts: posted ? posted.getAttribute('datetime') : null,
    // The listing's own state, which is the only geographic proof available for a post whose area
    // we never chose — an adopted one.
    state: ((offers.availableAtOrFrom || {}).address || {}).addressRegion || '',
    postal_code: ((offers.availableAtOrFrom || {}).address || {}).postalCode || '',
  };
}"""


# What the seller already has posted, read off `accounts.craigslist.org/login/home`.
#
# **Captured twice, because the page has two shapes and the first one misled us.** Signed in with
# nothing posted there is no table at all — the page says `no postings` in words. Signed in with a
# posting there is a real table whose cells are class-named, and reading *that* is what this does:
#
#     tr.posting-row
#       td.status              "Active"                              -> live, deterministically
#       td.title  > a[href]    the posting's canonical URL, and its title with the price appended
#       td.areacat             "sfo - sfc household items - by owner"
#       td.dates.posteddate    "09 Sep 2026 04:03"
#       td.dates.expdate       "29 days"                             -> how long it has left
#       td.postingID           "7963324125"                          -> craigslist's own post id
#
# The first version of this hunted anchors and regexed the row text, because the only prior art
# available described a table that no longer exists. It worked and it was fragile in three ways
# this is not: the status came from matching words anywhere in the row, the price came from the
# first `$` in it, and the title kept the price craigslist appends to the link text — which then
# travelled into the item's name and onto every other marketplace it was listed to.
#
# **An empty account is not an unreadable one, and telling them apart is the whole difficulty.**
# Absence of rows cannot mean "unparsed": read that way, a brand-new seller burns all five survey
# attempts in about five minutes of connecting and the market is abandoned permanently, having
# never asked them anything. That is not hypothetical — it happened on the first live run.
#
# Three answers, and only the last is retried:
#
#   rows found                      -> the listings
#   no rows, but "no postings" said -> an honest empty inventory, which closes the ask
#   neither                         -> `{error}`: signed out, or a shape we do not know
MY_LISTINGS_JS = """() => {
  const bodyText = ((document.body && document.body.innerText) || '');
  const rows = Array.from(document.querySelectorAll('tr.posting-row'));

  if (!rows.length) {
    // Craigslist says this in words on an account with nothing up. A fact about the inventory,
    // not a failure to read it — reported as an error it would retire the survey.
    if (/no postings/i.test(bodyText)) {
      return {
        listings: [], active_count: 0, dropped: 0, unreadable: 0, truncated: false,
        empty_stated: true,
      };
    }
    return {
      error: 'the account page showed neither postings nor "no postings"',
      chars: bodyText.length,
      visible: document.visibilityState === 'visible',
    };
  }

  const cell = (row, selector) => {
    const el = row.querySelector(selector);
    return el ? (el.innerText || '').trim() : '';
  };

  const listings = [];
  let unreadable = 0;
  let inactive = 0;

  rows.forEach((row) => {
    const postId = cell(row, 'td.postingID');
    const link = row.querySelector('td.title a[href]');
    if (!postId || !link) { unreadable += 1; return; }

    // `listing_id` must be whatever `LISTING_ID_PATTERN` extracts from the URL we *store*, because
    // that is how a later read of the same posting recognises it as already ours. Craigslist gives
    // a posting two addresses with two different ids in them — a canonical `/view/d/<slug>/<token>`
    // and an older `<city>…/<numeric>.html` — so agreement is not automatic, it is a choice about
    // which form is used consistently. Adoption stores what this cell links, which is the
    // canonical one, so the canonical token is the id and the publish records the same form.
    //
    // Getting this wrong is not a near-miss. On the first live run the publish stored the older
    // URL while this read reported the canonical token: the ids disagreed, the dedup found no
    // match, and the seller's own already-managed posting was adopted a second time. Both of the
    // survey's safety nets missed it — the title net excludes items that already hold a URL on
    // this market, on the assumption the id net caught them.
    const idFromUrl = String(link.getAttribute('href') || '').split('?')[0]
      .match(/\\/(?:view\\/)?d\\/[^/]+\\/([0-9]+|[A-Za-z0-9]{18,26})(?:\\.html)?\\/?$/);
    if (!idFromUrl) { unreadable += 1; return; }

    // Live only: everything downstream treats these as adoptable, and relisting something the
    // seller already took down is worse than missing it. The status is its own cell, so this is
    // not a search for words that might appear anywhere in the row.
    const status = cell(row, 'td.status').toLowerCase();
    if (status !== 'active') { inactive += 1; return; }

    // The link text is "<title> - $<price>": craigslist appends the price to the title it shows.
    // Kept, the price is stored as part of the name and travels onto every other marketplace.
    const shown = (link.innerText || '').trim();
    const priceText = (shown.match(/\\$\\s?[\\d,]+(?:\\.\\d{2})?\\s*$/) || [''])[0].trim();
    const title = shown.replace(/\\s*[-–—]\\s*\\$\\s?[\\d,]+(?:\\.\\d{2})?\\s*$/, '').trim();
    const price = Number(priceText.replace(/[^0-9.]/g, ''));
    if (!title || !Number.isFinite(price) || price <= 0) { unreadable += 1; return; }

    listings.push({
      listing_id: idFromUrl[1],
      // Craigslist's own number for the posting, which the canonical URL does not carry. Not the
      // join key — see above — but the one id that appears on every surface it has, so it is what
      // a human matches against the site and what a later liveness read can confirm.
      post_id: postId,
      url: link.href,
      title: title,
      price: price,
      price_text: priceText,
      // Craigslist has no sold state and free postings expire in weeks, so this is the only
      // warning anyone gets that a managed listing is about to stop existing.
      expires_in: cell(row, 'td.expdate'),
      posted: cell(row, 'td.posteddate'),
      area: cell(row, 'td.areacat'),
    });
  });

  // Every row on the page was examined, so the count *is* the denominator — there is no separate
  // tally to disagree with and nothing paginated out of view that a tally would have revealed.
  return {
    listings: listings,
    active_count: listings.length + inactive,
    dropped: inactive + unreadable,
    unreadable: unreadable,
    truncated: false,
  };
}"""


# A posting's *current* contact address — the probe that measured how relay addresses behave.
#
# **This is not where a reply to a buyer goes.** It reads a `sale.` address, which is what a buyer
# writes to in order to reach the seller. A reply must go to the `reply.` address the buyer's own
# message arrived from; sending to a freshly-read `sale.` address mails the seller their own reply.
# The name of this artifact was wrong for exactly that reason before it was corrected.
#
# **What it measured, on one live posting (id 7963439877) on 2026-09-10 — five distinct addresses:**
#
#     4cd598c01d62398ba33718c2199ba1c0   the address a buyer's message arrived on
#     65acaebd5a6532cfa2f291cd47e939c3   revealed to a logged-out viewer
#     69d7b648f566356f9b1740368d905ef8   revealed to the seller's own signed-in session
#     effa3e5a63223750ab7a013986bc3f92   \ two consecutive reloads of the same page,
#     913041a5e86f365fb6ac7393780ff341   /  seconds apart
#
# Craigslist mints a fresh address per *view*. Two claims made from a single earlier reading were
# both wrong and are recorded here so they are not made again: the hex does **not** belong to the
# posting, and the address is **not** hidden from the owner — the seller's own session revealed one
# fine. There is likewise no reliable signed-in signal on a posting page (`a[href*="logout"]` is
# absent in both contexts), so nothing here reports one.
#
# **Requires the panel to be open.** Craigslist loads the address by AJAX when "reply" and then the
# "email" option are clicked, so the caller clicks both and this reads the result. It answers
# `{error}` when the panel is closed rather than guessing, and says which affordance was missing so
# a page that is not a posting at all is distinguishable from one that simply needs another click.
#
# No lane calls this yet. It is kept because it is the only way to observe relay-address behaviour,
# and the liveness work in the plan needs exactly that.
POSTING_CONTACT_JS = """() => {
  const link = document.querySelector('a[href^="mailto:"]');
  if (!link) {
    return {
      error: 'no contact address on the page — the reply panel is not open',
      has_reply_button: !!document.querySelector('.reply-button'),
      has_email_option: !!document.querySelector('.reply-option-header'),
    };
  }

  const href = link.getAttribute('href') || '';
  const body = href.slice('mailto:'.length);
  const address = decodeURIComponent(body.split('?')[0] || '').trim().toLowerCase();
  if (!address || address.indexOf('@') === -1) {
    return { error: 'the reply link carried no address', href_chars: href.length };
  }

  // Craigslist seeds the mailto with the posting's title and its permalink. The permalink is the
  // only thing about a relay conversation that does not rotate, so it is worth carrying back.
  const query = body.indexOf('?') === -1 ? '' : body.slice(body.indexOf('?') + 1);
  const field = (name) => {
    const found = query.split('&').filter((p) => p.indexOf(name + '=') === 0)[0];
    return found ? decodeURIComponent(found.slice(name.length + 1).replace(/\\+/g, ' ')) : '';
  };

  return {
    address: address,
    subject: field('subject'),
    posting_url: (field('body').match(/https:\\/\\/[^\\s]*craigslist\\.org\\/[^\\s]+/) || [''])[0],
  };
}"""
