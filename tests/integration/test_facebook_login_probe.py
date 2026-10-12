"""Telling a signed-out Facebook from a signed-in one, run as JavaScript.

Skipped without node on PATH. Run for real because every lane test stubs the probe, and would agree
with whatever the Python side assumed the page looked like.

The case this file exists for is the page Facebook served on 2026-09-30 when it ended the agent's
session: not a login form and not a redirect, but its "Continue as" profile chooser, rendered in
place at the very address that was asked for (`/messages/t/<id>/`). It has no password field until
a profile is picked, so the probe answered `unknown` and the lane went on loading Facebook every few
minutes for eleven hours. The elements below are the ones that page's accessibility snapshot
recorded.

The dangerous direction is still the one that matters most: a false `logged_out` stops a
signed-in seller's market. So the signed-in page here carries a buyer quoting that chooser word for
word, link and all.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from sellee.browser.markets.facebook import LOGIN_JS

node_binary = shutil.which("node")

pytestmark = pytest.mark.skipif(node_binary is None, reason="needs node on PATH to run the probe")

# Just enough of `querySelector` for the probe: comma lists of `tag[attr op "value"]`, where op is
# one of = *= ^= $=, or a bare `[attr]`. The page is described as elements rather than as the
# selectors the probe happens to use, so rewording a selector cannot quietly keep a test passing.
_MATCHER = r"""
const matches = (el, simple) => {
  const m = /^([a-z]*)((?:\[[^\]]+\])*)$/.exec(simple.trim());
  if (!m) throw new Error('unsupported selector: ' + simple);
  if (m[1] && el.tag !== m[1]) return false;
  const attrs = m[2].match(/\[[^\]]+\]/g) || [];
  return attrs.every((part) => {
    const a = /^\[([\w-]+)(?:([*^$]?=)"([^"]*)")?\]$/.exec(part);
    if (!a) throw new Error('unsupported attribute selector: ' + part);
    const value = el.attrs[a[1]];
    if (value === undefined) return false;
    if (!a[2]) return true;
    if (a[2] === '=') return value === a[3];
    if (a[2] === '*=') return value.includes(a[3]);
    if (a[2] === '^=') return value.startsWith(a[3]);
    return value.endsWith(a[3]);
  });
};
const querySelectorAll = (elements) => (selector) =>
  elements.filter((el) => selector.split(',').some((s) => matches(el, s)));
const querySelector = (elements) => (selector) => querySelectorAll(elements)(selector)[0] || null;
"""


def _probe(elements: list, text: str = "") -> str:
    script = f"""
      {_MATCHER}
      const elements = {json.dumps(elements)}.map((el) => ({{
        ...el,
        getClientRects: () => (el.hidden ? [] : [{{ width: 10, height: 10 }}]),
      }}));
      global.document = {{
        body: {{ innerText: {json.dumps(text)} }},
        querySelector: querySelector(elements),
        querySelectorAll: querySelectorAll(elements),
      }};
      console.log(JSON.stringify(({LOGIN_JS})()));
    """
    out = subprocess.run(
        [node_binary, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)["state"]


def _el(tag: str, *, hidden: bool = False, **attrs: str) -> dict:
    """One element; `hidden` is one in the DOM that is not rendered, which `querySelector` still
    finds."""
    return {
        "tag": tag,
        "hidden": hidden,
        "attrs": {k.replace("_", "-"): v for k, v in attrs.items()},
    }


# The profile chooser Facebook served in place of `/messages/t/<id>/` on 2026-09-30.
CHOOSER = [
    _el("div", role="button", aria_label="Remove profiles from this browser"),
    _el("div", role="button", aria_label="Continue Jerry Neo Neo"),
    _el("div", role="button", aria_label="Use another profile"),
    _el("a", href="/reg/?entry_point=aymh&next=https%3A%2F%2Fwww.facebook.com%2Fmessages%2F"),
    _el("a", href="https://www.facebook.com/reg/"),
    _el("a", href="https://www.facebook.com/login/"),
]
CHOOSER_TEXT = (
    "Explore the things you love.\nJerry Neo Neo\nContinue\nUse another profile\n"
    "Create new account\nSign up\nLog in\nMessenger"
)

# A signed-in conversation: the chat rail and the composer.
SIGNED_IN = [
    _el("a", href="/messages/t/2445863962575993/"),
    _el("div", role="textbox", contenteditable="true"),
]


def test_the_profile_chooser_facebook_signs_an_account_out_to_is_logged_out() -> None:
    assert _probe(CHOOSER, CHOOSER_TEXT) == "logged_out"


def test_the_chooser_is_logged_out_whatever_address_it_was_served_at() -> None:
    """It came back at the conversation's own address, so nothing in the answer may lean on the
    URL — the elements the page offers are the evidence."""
    assert _probe(CHOOSER) == "logged_out"


def test_the_login_form_is_still_logged_out() -> None:
    assert _probe([_el("input", name="pass", type="password")]) == "logged_out"


def test_a_buyer_quoting_the_chooser_does_not_sign_the_seller_out() -> None:
    """A buyer can paste anything into a conversation, including Facebook's own sign-up link,
    which Messenger renders as a real link. It only renders on a signed-in page, though, and the
    signed-in markers are asked about first."""
    quoted = [
        *SIGNED_IN,
        _el("a", href="https://www.facebook.com/reg/?entry_point=aymh"),
        _el("a", href="/reg/"),
    ]
    assert _probe(quoted, "Continue Jerry Neo Neo\nUse another profile\nCreate new account") == (
        "logged_in"
    )


def test_a_page_that_proves_neither_is_unknown() -> None:
    """Unknown is the honest answer to a page we cannot place, and it is not a sign-out."""
    assert _probe([_el("a", href="https://www.facebook.com/marketplace/")]) == "unknown"


def test_a_link_that_merely_mentions_reg_is_not_a_sign_up_link() -> None:
    assert _probe([_el("a", href="https://www.facebook.com/groups/reg-car-club/")]) == "unknown"


def test_a_sign_up_link_that_is_not_rendered_proves_nothing() -> None:
    """A signed-in page can carry markup it has not shown — a menu built ahead of being opened —
    and `querySelector` finds it all the same. Only a link the page is actually offering counts."""
    signed_in_but_quiet = [
        _el("a", href="https://www.facebook.com/marketplace/"),
        _el("a", href="/reg/?entry_point=aymh", hidden=True),
    ]
    assert _probe(signed_in_but_quiet) == "unknown"
