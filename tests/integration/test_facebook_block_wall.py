"""Telling a marketplace refusing us from a buyer talking about one, run as JavaScript.

Skipped without node on PATH — and it has to run for real, because every other test in the suite
stubs the browser and would happily agree with whatever the detector's Python-side stub said.

What this file exists to hold is the dangerous direction. The block this feeds is durable and stops
every Facebook lane, so a phrase a buyer can type is not a false positive, it is a self-inflicted
outage on the seller's account. Hence the two-part test the detector keeps: a phrase AND a
prompt-shaped element. `SAID` is where that gets proved.

The wording in `WALLS` is the dialog Facebook actually served this seller's account on 2026-09-09.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from sellee.browser.markets.facebook import BLOCK_WALL_JS

node_binary = shutil.which("node")

pytestmark = pytest.mark.skipif(node_binary is None, reason="needs node on PATH to run the wall")

# The interstitial, verbatim. Heading and body are separate elements on the page, and
# `body.innerText` joins them with a newline — so this is stored the way the page renders it and
# every phrase the detector matches has to sit inside one of these lines, not across two.
AUTOMATION_WALL = (
    "We suspect automated behavior on your account\n"
    "To prevent your account from being temporarily restricted or permanently disabled, ensure "
    "that no other users or tools have access to your account and that you're following our Terms "
    "of Use. Also consider changing your password to a stronger one to prevent unauthorized access "
    "to your account by third parties.\n"
    "Dismiss"
)

PIN_WALL = "Enter your PIN\nEnter your PIN to restore your chats from this device."


def _wall(*, pathname="/messages/", text="", dialog=False, field=False) -> str:
    script = f"""
      global.location = {{ pathname: {json.dumps(pathname)} }};
      global.document = {{
        body: {{ innerText: {json.dumps(text)} }},
        querySelector: (sel) =>
          sel === '[role="dialog"]' ? ({json.dumps(dialog)} ? {{}} : null)
                                    : ({json.dumps(field)} ? {{}} : null),
      }};
      console.log(String(({BLOCK_WALL_JS})()));
    """
    out = subprocess.run(
        [node_binary, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return out.stdout.strip()


# --- what must be caught --------------------------------------------------------------------------


def test_the_warning_facebook_actually_served_is_recognised() -> None:
    assert _wall(text=AUTOMATION_WALL, dialog=True) == "automation"


def test_british_spelling_is_recognised_too() -> None:
    """Which spelling a seller's locale is served is not ours to assume."""
    assert _wall(text=AUTOMATION_WALL.replace("behavior", "behaviour"), dialog=True) == "automation"


@pytest.mark.parametrize(
    "pathname",
    [
        "/checkpoint/",
        "/checkpoint/1501092823525282/",
        # Served from under other roots too, which a prefix test would have missed entirely.
        "/login/checkpoint/",
        "/security/checkpoint/1501092823525282/",
    ],
)
def test_a_checkpoint_needs_no_words_at_all(pathname) -> None:
    """The one signal here nothing a person writes can forge, which is why it alone is unpaired."""
    assert _wall(pathname=pathname, text="") == "checkpoint"


@pytest.mark.parametrize("pathname", ["/mycheckpoints", "/checkpoints-explained", "/messages/"])
def test_a_path_that_merely_contains_the_word_is_not_a_checkpoint(pathname) -> None:
    """Anchored on the separators, so the clause stays the unforgeable one."""
    assert _wall(pathname=pathname, text="") == ""


def test_the_pin_wall_still_answers_as_itself() -> None:
    assert _wall(text=PIN_WALL, field=True) == "verify"


# --- what must never be caught --------------------------------------------------------------------

# Things a buyer, or a scammer, can type into a conversation. Every one of these reaches
# `body.innerText` exactly as a wall's own words would, and a block here would stop the market.
SAID = [
    "is this automated? you reply so fast",
    "we suspect automated behavior on your account",  # a buyer quoting it back at us
    "my account was temporarily restricted last week, so annoying",
    "just following our terms of use mate",
    "no other users or tools have access to your account right?",
    "ensure that no other users or tools have access to your account",
    "can you confirm your identity please, sending money is risky",
    "enter your pin when you collect, the gate needs it",
    "i'll go to the checkpoint near the mall",
]


@pytest.mark.parametrize("said", SAID)
def test_a_buyer_cannot_block_the_market_by_typing(said) -> None:
    """No dialog on screen, so none of this counts — a conversation is not a wall."""
    assert _wall(text=said) == ""


# What a buyer can still provoke with a dialog open, and what it is allowed to cost.
#
# Facebook opens dialogs of its own constantly, so the pairing only narrows a phrase as far as the
# phrase itself is narrow. The automation sentences are long enough that quoting one verbatim is
# reproducing the wall; the PIN wall's phrases are not — "confirm your identity" and "enter your
# pin" are things people type, and this is what decides that `verify` may queue a notice and must
# never take a durable block. See `blindness.BLOCKING_CAUSES`.
_PROVOKABLE = {
    "we suspect automated behavior on your account": "automation",
    "ensure that no other users or tools have access to your account": "automation",
    "can you confirm your identity please, sending money is risky": "verify",
    "enter your pin when you collect, the gate needs it": "verify",
}


@pytest.mark.parametrize("said", SAID)
def test_what_a_buyer_can_provoke_with_a_dialog_open(said) -> None:
    assert _wall(text=said, dialog=True) == _PROVOKABLE.get(said, "")


def test_an_ordinary_marketplace_inbox_is_not_a_wall() -> None:
    assert _wall(text="Marketplace\nGerry\nis this still available?\nSent 5m ago") == ""


def test_a_page_with_nothing_on_it_is_not_a_wall() -> None:
    """The empty case matters: a read that arrives before the page paints must not read as blocked
    and stop the market on a timing accident."""
    assert _wall(text="") == ""
