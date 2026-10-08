"""Craigslist's pages. There is no chat: buyers mail the registration address. The account is the
seller's own; what is read here is whether it is signed in, and what is driven is posting."""

from __future__ import annotations

import re

# Logged out it redirects to the login page, which also carries the sign-up form.
ACCOUNT_URL = "https://accounts.craigslist.org/login/home"

# Never guesses logged_out, which asks the seller to sign in and holds every post: it needs a
# password field in the accounts login form. Anything without positive evidence is unknown.
# Signed in, it names the account's email when the page shows it, and "" when it does not.
LOGIN_JS = """() => {
  try {
    const seen = (selector) => !!document.querySelector(selector);
    const password = seen('input[type="password"]');
    const loginForm = Array.from(document.querySelectorAll('form')).some((form) =>
      /accounts\\.craigslist\\.org\\/login/.test(form.getAttribute('action') || ''));
    if (password && (loginForm || seen('#inputEmailHandle'))) return { state: 'logged_out' };
    const logout = Array.from(document.querySelectorAll('a')).some((a) =>
      /\\/logout/.test(a.getAttribute('href') || '') ||
      /^log ?out$/.test((a.innerText || '').trim().toLowerCase()));
    if (logout || seen('form.manage') || seen('input[name="crypt"]')) {
      // "home of <email>" on the account page, "logged in as <email>" on a posting page.
      const named = Array.from(document.querySelectorAll('a[href*="/login/home"]'))
        .map((a) => (a.innerText || '').match(/[\\w.+-]+@[\\w-]+(\\.[\\w-]+)+/))
        .find(Boolean);
      return { state: 'logged_in', email: named ? named[0].toLowerCase() : '' };
    }
    return { state: 'unknown' };
  } catch (e) {
    return { state: 'unknown' };
  }
}"""

# --- posting -------------------------------------------------------------------------------------

# The site is opened by its code, never picked from the IP: a VPN exit in San Francisco was put
# on Craigslist's Egypt site. Every step is served on one URL, named by its `?s=`.
SITE = "sfo"
SITE_NAME = "SF bay area"
POST_URL = f"https://post.craigslist.org/c/{SITE}"
FOR_SALE_BY_OWNER = "for sale by owner"
# Its catch-all by-owner category, as Facebook's driver files under "Miscellaneous".
DEFAULT_CATEGORY = "general for sale"
# Only on a multi-area site's neighborhood step; the step offers it as its first choice.
BYPASS_HOOD = "bypass this step"
# Only cars, motorcycles and RVs carry a fee in the US, and it shows in the label.
FEE_LABEL = r"\(\$\d"

# The posting URL, up to eight step submits and the manage page.
PUBLISH_LOADS = 10

CHOICE_ATTR = "data-sellee-cl"
CHOICE = f"[{CHOICE_ATTR}=choice]"
CONTINUE = "form button[type=submit][name=go]"
# The map step's continue has no name; its find button does.
MAP_CONTINUE = "form:has(#search_button) button[type=submit]:not(#search_button)"
TITLE = "#PostingTitle"
PRICE = "form input[name=price]"
ZIP = "form input[name=postal]"
BODY = "#PostingBody"
CONDITION = "form select[name=condition]"
# The select is hidden behind a jQuery UI selectmenu; a person opens its button and picks an item.
CONDITION_OPEN = f"[{CHOICE_ATTR}=condition-open]"
CONDITION_ITEM = f"[{CHOICE_ATTR}=condition-item]"
CONDITION_OPEN_MARK_JS = f"""() => {{
  const select = document.querySelector('{CONDITION}');
  const button = select && document.getElementById(select.id + '-button');
  if (button) button.setAttribute('{CHOICE_ATTR}', 'condition-open');
  return {{ marked: !!button }};
}}"""
CONDITION_READ_JS = f"""() => {{
  const select = document.querySelector('{CONDITION}');
  const option = select && select.options[select.selectedIndex];
  return {{ shown: option ? option.text.trim().toLowerCase() : '' }};
}}"""


def condition_item_js(wanted: str) -> str:
    """Marks the open condition menu's item labelled `wanted`, ignoring case."""
    return f"""() => {{
  const wanted = {wanted.strip().lower()!r};
  const select = document.querySelector('{CONDITION}');
  const menu = select && document.getElementById(select.id + '-menu');
  const items = menu ? [...menu.querySelectorAll('li.ui-menu-item')] : [];
  const item = items.find((li) => li.innerText.trim().toLowerCase() === wanted);
  if (item) item.setAttribute('{CHOICE_ATTR}', 'condition-item');
  return {{ marked: !!item, options: items.map((li) => li.innerText.trim()) }};
}}"""


CHAT = "form input[name=contact_chat_ok]"
ADD_IMAGES = "#plupload"
DONE_WITH_IMAGES = "#doneWithImages"
# The preview has two identical publish forms; the first is marked so a click has one target.
PUBLISH = f"[{CHOICE_ATTR}=publish]"
PUBLISH_MARK_JS = f"""() => {{
  const button = document.querySelector('form:has(input[name=continue][value=y]) button[name=go]');
  if (button) button.setAttribute('{CHOICE_ATTR}', 'publish');
  return {{ marked: !!button }};
}}"""
# The account's terms form, or the posting flow's terms step with two identical ACCEPT buttons.
TERMS = f"[{CHOICE_ATTR}=terms]"
TERMS_MARK_JS = f"""() => {{
  const account = 'form:has(input[name=step][value=touAccepted]) [type=submit]';
  const button = document.querySelector(account)
    || document.querySelector('button[name=continue][value=y]');
  if (button) button.setAttribute('{CHOICE_ATTR}', 'terms');
  return {{ marked: !!button }};
}}"""

# Why Craigslist sent a form back, e.g. "All postings must have a description".
REFUSAL_JS = """() => {
  const texts = [...document.querySelectorAll('[class*="error"]')]
    .map((el) => (el.innerText || '').trim().replace(/\\s+/g, ' '))
    .filter((text) => /required|must|missing|invalid|incorrect/i.test(text));
  texts.sort((a, b) => b.length - a.length);
  return { text: (texts[0] || '').slice(0, 300) };
}"""

STEP_JS = """() => {
  try {
    const params = new URLSearchParams(location.search);
    const terms = location.pathname.startsWith('/login/tou') || params.get('s') === 'tou';
    const step = terms ? 'terms' : params.get('s') || '';
    const confirmed = /posting confirmation/i.test(document.title);
    const loggedIn = !!document.querySelector('a[href*="/logout"]');
    const images = ((document.body && document.body.innerText) || '')
      .match(/this posting has (\\d+) images?/i);
    return {
      step: confirmed ? 'confirmed' : step,
      site: (document.title.split('|')[0] || '').trim(),
      logged_in: loggedIn,
      images: images ? Number(images[1]) : null,
    };
  } catch (e) {
    return { step: '', logged_in: false, images: null };
  }
}"""


def choice_js(wanted: str) -> str:
    """Marks the step's radio whose label is `wanted`, ignoring case. Exact, because the first
    label on the category step wraps the whole list."""
    return f"""() => {{
  const wanted = {wanted.strip().lower()!r};
  document.querySelectorAll('[{CHOICE_ATTR}]').forEach((el) => el.removeAttribute('{CHOICE_ATTR}'));
  const options = [];
  let chosen = false;
  for (const radio of document.querySelectorAll('form input[type=radio]')) {{
    const label = ((radio.closest('label') || radio.parentElement || {{}}).innerText || '').trim();
    options.push(label);
    if (!chosen && label.toLowerCase() === wanted) {{
      radio.setAttribute('{CHOICE_ATTR}', 'choice');
      chosen = true;
    }}
  }}
  return {{ chosen, options }};
}}"""


# Marks the one radio or button on the copy-from-another step that starts a new posting; zero or
# several matches mark nothing, and the labels seen come back so the real wording can be copied.
NEW_POSTING_JS = f"""() => {{
  document.querySelectorAll('[{CHOICE_ATTR}]').forEach((el) => el.removeAttribute('{CHOICE_ATTR}'));
  const fresh = /(new posting|new post|start (a )?new|from scratch|don.?t copy|no,? thanks)/i;
  const controls = Array.from(document.querySelectorAll(
    'form input[type=radio], form button, form input[type=submit]'));
  const labelOf = (el) => (el.type === 'radio'
    ? ((el.closest('label') || el.parentElement || {{}}).innerText || '')
    : (el.innerText || el.value || '')).trim();
  const options = controls.map(labelOf).filter(Boolean);
  const matches = controls.filter((el) => fresh.test(labelOf(el)));
  if (matches.length !== 1) return {{ chosen: false, options }};
  matches[0].setAttribute('{CHOICE_ATTR}', 'choice');
  return {{ chosen: true, radio: matches[0].type === 'radio', options }};
}}"""


# Marks the neighborhood step's bypass radio, whose label opens with the step's own question.
HOOD_BYPASS_JS = f"""() => {{
  for (const radio of document.querySelectorAll('form input[type=radio]')) {{
    const label = ((radio.closest('label') || radio.parentElement || {{}}).innerText || '');
    if (/{BYPASS_HOOD}/i.test(label)) {{
      radio.setAttribute('{CHOICE_ATTR}', 'choice');
      return {{ chosen: true }};
    }}
  }}
  return {{ chosen: false }};
}}"""

EDIT_READBACK_JS = f"""() => {{
  const value = (sel) => {{ const el = document.querySelector(sel); return el ? el.value : null; }};
  const chat = document.querySelector({CHAT!r});
  return {{
    title: value({TITLE!r}),
    price: value({PRICE!r}),
    zip: value({ZIP!r}),
    description: value({BODY!r}),
    chat_on: !!(chat && chat.checked),
  }};
}}"""

# The preview renders the post as a buyer sees it: "<title> - $<price> (<area>)".
PREVIEW_TEXT_JS = """() => ({
  text: ((document.body && document.body.innerText) || '').slice(0, 8000),
})"""

MANAGE_LINK_JS = """() => {
  const a = document.querySelector('a[href^="https://post.craigslist.org/manage/"]');
  return { url: a ? a.href : null };
}"""

# The manage page names the post's canonical address, the one buyer mail quotes.
MANAGED_POST_JS = """() => {
  const a = document.querySelector('a[href^="https://www.craigslist.org/view/d/"]');
  const text = (document.body && document.body.innerText) || '';
  const id = text.match(/post id:\\s*(\\d+)/i);
  return { url: a ? a.href : null, post_id: id ? id[1] : null, text };
}"""

# --- editing ------------------------------------------------------------------------------------

MANAGE_URL = "https://post.craigslist.org/manage/{token}"
EDIT_TEXT = "form.manage.edittext [name=go]"
EDIT_IMAGES = "form.manage.editimage [name=go]"
# What the driver can change; category, type and site are fixed once posted.
EDITABLE_FIELDS = frozenset({"title", "list_price", "description", "photos"})
# A text and a photo edit together: each opens the manage page, submits its steps and publishes,
# then reads the manage page again, the photo edit reopening its images to count them.
EDIT_LOADS = 14
# A text-only edit: the manage page, the edit form, the preview, publish, the manage page again.
EDIT_TEXT_LOADS = 6
DELETE_IMAGE = f"[{CHOICE_ATTR}=delete]"
# Marks the first image's own remove button on the images step: an in-page form, never the
# manage page's "Delete this Posting".
DELETE_IMAGE_MARK_JS = f"""() => {{
  for (const el of document.querySelectorAll('[{CHOICE_ATTR}=delete]')) {{
    el.removeAttribute('{CHOICE_ATTR}');
  }}
  const button = document.querySelector('form.delete.ajax button[name=go]');
  if (button) button.setAttribute('{CHOICE_ATTR}', 'delete');
  return {{ marked: !!button }};
}}"""


def manage_url(listing_url: str) -> str:
    """The manage page of the post at this canonical address: both carry the post's token."""
    return MANAGE_URL.format(token=listing_url.rstrip("/").rsplit("/", 1)[-1])


# --- categories ---------------------------------------------------------------------------------

# By-owner categories as the SF bay area category step labels them, with the words that place an
# item there. Fee, vehicle, "wanted", "free" and "barter" categories are never chosen.
CATEGORY_WORDS = {
    "antiques": "antique|vintage|victorian|art deco",
    "appliances": (
        "appliance|fridge|refrigerator|freezer|washer|dryer|dishwasher|microwave|oven|stove|"
        "air conditioner|vacuum|blender|kettle|toaster|air fryer|coffee maker|espresso"
    ),
    "arts & crafts": "craft|yarn|sewing|fabric|paint|easel|canvas|cricut",
    "auto parts": "car part|auto part|bumper|headlight|brake|exhaust|radiator",
    "auto wheels & tires": "tire|tires|rim|rims|wheel set",
    "baby & kid stuff": "baby|stroller|crib|toddler|car seat|high chair|infant",
    "bicycle parts": "bike part|derailleur|pedals|saddle|handlebar|bike helmet",
    "bicycles": "bicycle|bike|e-bike|ebike|road bike|mountain bike",
    "books & magazines": "book|books|novel|magazine|textbook|comic",
    "cds / dvds / vhs": "cd|cds|dvd|dvds|blu-ray|vhs|vinyl|record",
    "cell phones": "phone|iphone|smartphone|android|pixel|galaxy|phone case|charger",
    "clothing & accessories": (
        "shirt|jacket|coat|dress|jeans|pants|shoes|sneakers|boots|hoodie|sweater|handbag|"
        "purse|backpack|wallet|sunglasses|hat"
    ),
    "collectibles": "collectible|figurine|funko|trading card|pokemon card|coin",
    "computer parts": (
        "gpu|graphics card|cpu|ram|ssd|hard drive|motherboard|keyboard|mouse|monitor|power supply"
    ),
    "computers": "laptop|macbook|computer|pc|desktop|chromebook|imac",
    "electronics": (
        "earbuds|buds|airpods|headphones|headset|speaker|bluetooth|wireless|tv|television|"
        "soundbar|tablet|ipad|kindle|smartwatch|watch|charger|projector|router|drone|"
        "electronic"
    ),
    "farm & garden": "garden|plant|planter|lawn|mower|hose|shovel|soil",
    "furniture": (
        "sofa|couch|chair|table|desk|dresser|bed|mattress|bookshelf|shelf|cabinet|nightstand|"
        "wardrobe|ottoman|stool"
    ),
    "health and beauty": "makeup|skincare|perfume|hair dryer|shaver|massager",
    "household items": (
        "lamp|rug|curtain|mirror|pan|pot|dishes|cookware|bedding|towel|storage|organizer|decor"
    ),
    "jewelry": "ring|necklace|bracelet|earrings|jewelry|pendant",
    "materials": "lumber|plywood|tiles|bricks|drywall|insulation",
    "motorcycle parts": "motorcycle part|motorcycle helmet",
    "musical instruments": (
        "guitar|piano|keyboard piano|drum|drums|violin|ukulele|amp|amplifier|synth|microphone"
    ),
    "photo/video": "camera|lens|tripod|gopro|dslr|mirrorless|gimbal",
    "sporting goods": (
        "golf|tennis|ski|skis|snowboard|surfboard|weights|dumbbell|treadmill|yoga|kayak|tent|"
        "camping|fishing|basketball|skateboard"
    ),
    "tickets": "ticket|tickets",
    "tools": "drill|saw|wrench|tool|tools|toolbox|sander|ladder",
    "toys & games": "toy|toys|lego|board game|puzzle|doll|action figure",
    "video gaming": "playstation|ps4|ps5|xbox|nintendo|switch|video game|controller|steam deck",
}
# Never more than this many categories per item: each one is a whole post.
MAX_CATEGORIES = 3
# Where the crosslist lane hands the driver the category it picked.
CATEGORY_KEY = "craigslist_category"


def categories_for(item: dict) -> list:
    """The categories to post an item in, best first, ending with the catch-all."""
    title = (item.get("title") or "").lower()
    description = (item.get("description") or "").lower()

    def hits(text: str, words) -> int:
        return sum(1 for word in words.split("|") if re.search(rf"\b{re.escape(word)}\b", text))

    scored = []
    for category, words in CATEGORY_WORDS.items():
        # The title names the thing; the description only tips a tie.
        score = 3 * hits(title, words) + hits(description, words)
        if score:
            scored.append((-score, category))
    ranked = [category for _, category in sorted(scored)][: MAX_CATEGORIES - 1]
    return [*ranked, DEFAULT_CATEGORY]


# --- a posted post, as any visitor sees it ------------------------------------------------------

POST_LIVE = "live"
POST_FLAGGED = "flagged"
POST_GONE = "gone"
# Not served, with no notice saying why: a new post can read this way before it is served.
POST_MISSING = "missing"
POST_UNKNOWN = "unknown"
# Craigslist's own removal notice; the seller's description is escaped, so it cannot forge one.
_NOTICE = re.compile(r'<div[^>]*\bclass="[^"]*\bremoved\b[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
_FLAGGED = re.compile(r"flagged for removal", re.I)
_GONE = re.compile(r"deleted by its author|this posting has expired", re.I)


def post_state(status: int, body: str) -> str:
    """What a post's public page says about it. Anything unexpected is unknown, which acts on
    nothing: a removal is only ever read from Craigslist's own notice or a gone status."""
    notice = " ".join(_NOTICE.findall(body or ""))
    if _FLAGGED.search(notice):
        return POST_FLAGGED
    if _GONE.search(notice):
        return POST_GONE
    if status in (404, 410):
        return POST_MISSING
    if status == 200:
        return POST_LIVE
    return POST_UNKNOWN


_CONDITIONS = ("new", "like new", "excellent", "good", "fair", "salvage")


def condition_for(said: str) -> str:
    """Craigslist's condition word for an item's free-text condition, or "" to leave it unset."""
    said = (said or "").strip().lower()
    if not said:
        return ""
    if said in _CONDITIONS:
        return said
    if said.startswith("new") or said == "brand new":
        return "new"
    if "like new" in said or "mint" in said:
        return "like new"
    if "fair" in said or "well used" in said or "heavily" in said:
        return "fair"
    if "poor" in said or "parts" in said:
        return "salvage"
    return "good"
