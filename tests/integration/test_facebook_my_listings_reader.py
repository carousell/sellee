"""Which listings on a Facebook profile are the seller's own, run as JavaScript.

Skipped without node on PATH. The reader runs in the page, so this drives the shipped artifact
against a small DOM shaped like the page the survey actually landed on.

The case this file exists for was captured on 2026-10-08. The seller's Marketplace profile opened
as a dialog over the Marketplace feed, and the seller had nothing listed: the dialog's listings
section said "No listings found". Climbing from the "<name>'s listings" heading to the first
ancestor holding any item link walked out of the dialog into the feed's "Today's picks" grid, so the
survey read eighteen strangers' cards as the seller's. Cards whose first line was a "Just listed"
badge would not parse, which is all that stopped it asking the seller to take over the rest. Every
look came back "unreadable" and the survey was abandoned. Structure only; all text is placeholder.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from sellee.browser.markets.facebook import MY_LISTINGS_JS

node_binary = shutil.which("node")

pytestmark = pytest.mark.skipif(node_binary is None, reason="needs node on PATH to run the reader")

# A tree of {tag, role, text, href, children}, turned into just enough DOM for the reader.
_DOM = r"""
const ORIGIN = 'https://www.facebook.com';
const tags = (sel) => sel.split(',').map((s) => s.trim());
const matches = (el, sel) => tags(sel).some((one) => {
  const m = one.match(/^([a-z0-9]*)(?:\[([a-z-]+)(\*=|\^=|=)"([^"]*)"\])?$/i);
  if (!m) throw new Error('selector the stub does not know: ' + one);
  const [, tag, attr, op, value] = m;
  if (tag && el.tag !== tag) return false;
  if (!attr) return true;
  const got = el.getAttribute(attr);
  if (got === null) return false;
  if (op === '=') return got === value;
  if (op === '^=') return got.startsWith(value);
  return got.includes(value);
});
const build = (spec, parent) => {
  const el = {
    tag: spec.tag || 'div',
    parentElement: parent,
    children: [],
    getAttribute: (name) =>
      name === 'role' ? spec.role || null : name === 'href' ? spec.href || null : null,
    scrollIntoView: () => {},
    getClientRects: () => [{}],
  };
  if (spec.href) el.href = ORIGIN + spec.href;
  el.children = (spec.children || []).map((child) => build(child, el));
  const all = () => el.children.flatMap((c) => [c, ...c.all()]);
  el.all = all;
  Object.defineProperty(el, 'innerText', {
    get: () => [spec.text || '', ...el.children.map((c) => c.innerText)]
      .filter(Boolean).join('\n'),
  });
  el.querySelectorAll = (sel) => all().filter((c) => matches(c, sel));
  el.querySelector = (sel) => el.querySelectorAll(sel)[0] || null;
  return el;
};
const body = build({ tag: 'body', children: %(page)s }, null);
global.window = { innerWidth: 1600 };
global.location = { pathname: '/marketplace/profile/1/' };
global.document = {
  visibilityState: 'visible',
  body: body,
  querySelector: body.querySelector,
  querySelectorAll: body.querySelectorAll,
};
"""


def _read(page: list) -> dict:
    script = (
        _DOM % {"page": json.dumps(page)}
        + f"\n({MY_LISTINGS_JS})().then((r) => console.log(JSON.stringify(r)));"
    )
    out = subprocess.run(
        [node_binary, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)


def _card(listing_id: str, *lines: str) -> dict:
    return {
        "tag": "a",
        "href": f"/marketplace/item/{listing_id}/?ref=browse_tab",
        "children": [{"text": line} for line in lines],
    }


# Other people's listings, as the feed under the dialog renders them.
FEED = {
    "tag": "main",
    "children": [
        {"tag": "h2", "text": "Today's picks"},
        {
            "children": [
                _card("9001", "€29,500", "Some town"),
                _card("9002", "Just listed", "€400", "Someone's thing", "Some town"),
                _card("9003", "€500", "Another thing", "Some town"),
            ]
        },
    ],
}


def _profile(grid: dict | None) -> dict:
    """The profile dialog: a rating section, then the listings section holding `grid`."""
    section = [
        {
            "children": [
                {"tag": "h2", "text": "Seller's listings"},
                {"text": "You can manage listings from"},
                {"tag": "a", "href": "/marketplace/you/selling/", "text": "Your listings"},
            ]
        }
    ]
    if grid is not None:
        section.append(grid)
    return {
        "role": "dialog",
        "children": [
            {"children": [{"text": "Seller name"}, {"text": "Joined Facebook in 2026"}]},
            {"children": [{"tag": "h2", "text": "Rating and strengths"}]},
            {"children": section},
        ],
    }


def _ids(answer: dict) -> list:
    return [row["listing_id"] for row in answer["listings"]]


def test_an_empty_profile_over_the_feed_is_nothing_listed() -> None:
    """The page as captured: the dialog says it has nothing, the feed beneath has strangers."""
    answer = _read([FEED, _profile({"children": [{"text": "No listings found"}]})])

    assert answer["listings"] == []
    assert answer["unreadable"] == 0
    assert answer["truncated"] is False


def test_the_sellers_own_grid_in_the_dialog_is_read_and_the_feed_is_not() -> None:
    grid = {
        "children": [
            _card("101", "€20", "First placeholder"),
            _card("102", "Just listed", "€35", "Second placeholder"),
        ]
    }

    answer = _read([FEED, _profile(grid)])

    assert _ids(answer) == ["101", "102"]
    assert [row["price"] for row in answer["listings"]] == [20, 35]
    assert answer["listings"][1]["title"] == "Second placeholder"
    assert answer["unreadable"] == 0


def test_a_dialog_with_neither_a_grid_nor_an_empty_section_is_a_failed_read() -> None:
    """Not yet rendered is not nothing listed: an empty answer would close the survey for good."""
    answer = _read([FEED, _profile(None)])

    assert "listings" not in answer
    assert answer["error"]


def test_a_profile_rendered_as_a_page_still_reads_its_own_grid() -> None:
    """The layout the reader was written for: the seller's grid beside the feed, no dialog."""
    page = {
        "children": [
            {
                "children": [
                    {"tag": "h2", "text": "Seller's listings"},
                    {"children": [_card("201", "€15", "Placeholder")]},
                ]
            },
            FEED,
        ]
    }

    assert _ids(_read([page])) == ["201"]
