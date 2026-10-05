"""Craigslist's account pages. There is no chat: buyers mail the registration address, so what
is driven here is the account itself, located by stable ids and form fields."""

from __future__ import annotations

# Logged out it redirects to the login page, which also carries the sign-up form.
ACCOUNT_URL = "https://accounts.craigslist.org/login/home"
LOGIN_URL = "https://accounts.craigslist.org/login"
SIGN_UP_EMAIL = "#emailAddress"
SIGN_UP_BUTTON = "#create"
# The activation page offers two forms; the one without a password field is "Go Passwordless".
GO_PASSWORDLESS = "form:not(:has(#inputNewPassword)) [type=submit]"
ACCEPT_TERMS = "form:has(input[name=step][value=touAccepted]) [type=submit]"

LOGIN_JS = """() => {
  try {
    if (document.querySelector('a[href*="/logout"]')) return { state: 'logged_in' };
    if (document.querySelector('form.loginform')) return { state: 'logged_out' };
    return { state: 'unknown' };
  } catch (e) {
    return { state: 'unknown' };
  }
}"""

# Which account page this is: login, signup_sent, password_options, terms, account_home, unknown.
PAGE_JS = f"""() => {{
  try {{
    const path = location.pathname;
    const text = (document.body && document.body.innerText) || '';
    if (path.startsWith('/login/tou')) return {{ kind: 'terms' }};
    if (path === '/pass' && document.querySelector({GO_PASSWORDLESS!r})) {{
      return {{ kind: 'password_options' }};
    }}
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

# Craigslist picks the site from the IP and serves every step on one URL, named by its `?s=`.
POST_URL = "https://post.craigslist.org/c/"
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

STEP_JS = """() => {
  try {
    const step = new URLSearchParams(location.search).get('s') || '';
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
# The manage page, the edit or images steps, publish, and the manage page again; both at most.
EDIT_LOADS = 12
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
