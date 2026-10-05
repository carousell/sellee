"""Craigslist's account pages, as recorded by the 2026-10-05 spike.

Craigslist has no chat to read: its buyers arrive as mail to the registration address. What is
driven here is the account itself, on accounts.craigslist.org, where every control has a stable id
or a form field to locate it by.
"""

from __future__ import annotations

# Logged out it redirects to the login page, which also carries the sign-up form.
ACCOUNT_URL = "https://accounts.craigslist.org/login/home"
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
