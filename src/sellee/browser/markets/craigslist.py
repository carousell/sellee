"""Craigslist's account pages. There is no chat: buyers mail the registration address, so what
is driven here is the account itself, located by stable ids and form fields."""

from __future__ import annotations

# Logged out it redirects to the login page, which also carries the sign-up form.
ACCOUNT_URL = "https://accounts.craigslist.org/login/home"
LOGIN_URL = "https://accounts.craigslist.org/login"
SIGN_UP_EMAIL = "#emailAddress"
LOGIN_EMAIL = "#inputEmailHandle"
# "E-mail a login link": the login form, sent with no password.
LOGIN_LINK_BUTTON = "#onetime"
SIGN_UP_BUTTON = "#create"
# The activation page offers two forms; the one without a password field is "Go Passwordless".
# Live, a second submit on the page also had no password field; the button is named.
GO_PASSWORDLESS = 'input[type=submit][value="Go Passwordless"]'
ACCEPT_TERMS = "form:has(input[name=step][value=touAccepted]) [type=submit]"

# Never guesses logged_out, which emails a login link and holds every post: it needs a password
# field in the accounts login form. Anything without positive evidence either way is unknown.
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
      return { state: 'logged_in' };
    }
    return { state: 'unknown' };
  } catch (e) {
    return { state: 'unknown' };
  }
}"""

# Which account page this is: login, signup_sent, login_link_sent, password_options, terms,
# account_home or unknown.
PAGE_JS = f"""() => {{
  try {{
    const path = location.pathname;
    const text = (document.body && document.body.innerText) || '';
    if (path.startsWith('/login/tou')) return {{ kind: 'terms' }};
    if (path === '/pass' && document.querySelector({GO_PASSWORDLESS!r})) {{
      return {{ kind: 'password_options' }};
    }}
    if (/login link sent/i.test(text)) return {{ kind: 'login_link_sent' }};
    const sent = /link to activate your account has been emailed/i.test(text);
    if (path.startsWith('/signup') && sent) return {{ kind: 'signup_sent' }};
    if (document.querySelector('a[href*="/logout"]')) return {{ kind: 'account_home' }};
    if (document.querySelector('form.signupform')) return {{ kind: 'login' }};
    return {{ kind: 'unknown' }};
  }} catch (e) {{
    return {{ kind: 'unknown' }};
  }}
}}"""

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
