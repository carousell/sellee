"""Posts an item to Craigslist by driving its posting form one `?s=` step at a time, under
`publisher.publish`'s contract: not attempted before the publish click, unverified after it."""

from __future__ import annotations

import logging
import re

from sellee import marketplaces
from sellee.browser import craigslist_account, formfill
from sellee.browser import governor as page_governor
from sellee.browser.client import BrowserError, ControlMoved
from sellee.browser.markets import craigslist
from sellee.browser.publisher import (
    PublishNeedsSeller,
    PublishNotAttempted,
    PublishOutcome,
    PublishUnverified,
)

log = logging.getLogger(__name__)

# A US posting is at most eight steps; past this the form is looping.
MAX_STEPS = 12
# How long a submitted step, or an image upload, gets to show its next page.
TRANSITION_WAIT_SEC = 20.0
IMAGE_WAIT_SEC = 60.0
_POLL_SEC = 0.5

AREA_KEY = "craigslist_area"
ZIP_KEY = "zip"


def area_question(site: str, options: list, missed: str = "") -> str:
    lead = f"“{missed}” isn't one of Craigslist's areas. " if missed else ""
    return (
        f"{lead}Craigslist put you on its {site or 'local'} site. Which area are you in? "
        f"Reply with one of: {', '.join(options)}."
    )


def publish(client, item: dict, *, create_url: str, photos=(), seller: dict, sleep=None):
    pause = sleep or formfill.sleep
    try:
        _to_preview(client, item, create_url, photos, seller, pause)
    except (PublishNotAttempted, page_governor.PagesSpent):
        raise
    except BrowserError as exc:
        # Nothing has been published yet, so whatever failed, trying again is safe.
        raise PublishNotAttempted(f"could not fill in the post: {exc}", retryable=True) from exc
    return _commit(client, pause)


def _to_preview(client, item: dict, create_url: str, photos, seller: dict, pause) -> None:
    client.navigate_visible(create_url)
    for _ in range(MAX_STEPS):
        page = _read(client)
        if not page.get("logged_in"):
            raise PublishNotAttempted("Craigslist shows the account signed out", retryable=True)
        step = page.get("step") or ""
        if step == "preview":
            _check_preview(client, item)
            return
        act = _STEPS.get(step)
        if act is None:
            raise PublishNotAttempted(f"Craigslist showed a page sellee does not know: {step!r}")
        act(client, page, item, photos, seller, pause)
        _wait_past(client, step, pause)
    raise PublishNotAttempted("the posting form never reached its preview", retryable=True)


def _read(client) -> dict:
    return client.evaluate(craigslist.STEP_JS) or {}


def _wait_past(client, step: str, pause, wait_sec: float = TRANSITION_WAIT_SEC) -> str:
    """The step a submitted page led to; clicks return before the next page has loaded."""
    for _ in range(int(wait_sec / _POLL_SEC)):
        now = _read(client).get("step") or ""
        if now and now != step:
            return now
        pause(_POLL_SEC)
    raise PublishNotAttempted(f"Craigslist did not move past {step!r}", retryable=True)


def _submit(client, target: str, element: str) -> None:
    """Click a control that loads the next step: a page load the governor must allow."""
    client.pace(craigslist.POST_URL)
    client.click(target, element)


def _choose(client, label: str) -> dict:
    """Mark and click the step's radio called `label`, if it has one."""
    answer = client.evaluate(craigslist.choice_js(label)) or {}
    if answer.get("chosen"):
        client.click(craigslist.CHOICE, label)
    return answer


def _subarea(client, page, item, photos, seller, pause) -> None:
    area = str(seller.get(AREA_KEY) or "")
    answer = _choose(client, area) if area else client.evaluate(craigslist.choice_js("")) or {}
    if not answer.get("chosen"):
        options = [label for label in answer.get("options") or [] if label]
        raise PublishNeedsSeller(
            f"no Craigslist area called {area!r}",
            key=AREA_KEY,
            question=area_question(str(page.get("site") or ""), options, missed=area),
        )
    _submit(client, craigslist.CONTINUE, "continue")


def _hood(client, page, item, photos, seller, pause) -> None:
    if not (client.evaluate(craigslist.HOOD_BYPASS_JS) or {}).get("chosen"):
        raise PublishNotAttempted("the neighborhood step offered no way past it")
    client.click(craigslist.CHOICE, "bypass this step")
    _submit(client, craigslist.CONTINUE, "continue")


def _type(client, page, item, photos, seller, pause) -> None:
    _pick(client, craigslist.FOR_SALE_BY_OWNER)


def _cat(client, page, item, photos, seller, pause) -> None:
    _pick(client, craigslist.DEFAULT_CATEGORY)


def _pick(client, label: str) -> None:
    if re.search(craigslist.FEE_LABEL, label):
        raise PublishNotAttempted(f"{label!r} costs money to post in")
    if not _choose(client, label).get("chosen"):
        raise PublishNotAttempted(f"Craigslist offers no {label!r}")
    _submit(client, craigslist.CONTINUE, "continue")


def _edit(client, page, item, photos, seller, pause) -> None:
    zip_code = str(seller.get(ZIP_KEY) or "")
    if not zip_code:
        raise PublishNeedsSeller("no ZIP code", key=ZIP_KEY, question=craigslist_account.ZIP_NOTICE)
    title = (item.get("title") or "").strip()
    for target, element, text in (
        (craigslist.TITLE, "the posting title", title),
        (craigslist.PRICE, "the price", str(formfill.bare_price(item.get("list_price")))),
        (craigslist.ZIP, "the ZIP code", zip_code),
        (craigslist.BODY, "the description", item.get("description") or ""),
    ):
        if text:
            client.type_humanly(target, element, text)
            pause(formfill.FIELD_SETTLE_SEC)
    _set_condition(client, item)
    if (client.evaluate(craigslist.EDIT_READBACK_JS) or {}).get("chat_on"):
        client.click(craigslist.CHAT, "CL chat")
    seen = client.evaluate(craigslist.EDIT_READBACK_JS) or {}
    if seen.get("chat_on"):
        raise PublishNotAttempted("CL chat would not switch off; buyers must write by mail")
    if (seen.get("title") or "").strip() != title:
        raise PublishNotAttempted(f"the form shows the title as {seen.get('title')!r}")
    if not formfill.price_matches(seen.get("price"), item.get("list_price")):
        raise PublishNotAttempted(f"the form shows the price as {seen.get('price')!r}")
    if (seen.get("zip") or "").strip() != zip_code:
        raise PublishNotAttempted(f"the form shows the ZIP code as {seen.get('zip')!r}")
    _submit(client, craigslist.CONTINUE, "continue")


def _set_condition(client, item: dict) -> None:
    """Best-effort: Craigslist's condition is optional, and its select sits behind a widget."""
    wanted = craigslist.condition_for(str(item.get("condition") or ""))
    if not wanted:
        return
    try:
        client.call_tool(
            "browser_select_option",
            {"target": craigslist.CONDITION, "element": "the condition", "values": [wanted]},
        )
    except BrowserError:
        log.info("could not set the Craigslist condition", exc_info=True)


def _geoverify(client, page, item, photos, seller, pause) -> None:
    # The map is already placed from the ZIP code.
    _submit(client, craigslist.MAP_CONTINUE, "continue")


def _editimage(client, page, item, photos, seller, pause) -> None:
    if photos:
        client.click(craigslist.ADD_IMAGES, "Add Images")
        client.call_tool("browser_file_upload", {"paths": [str(path) for path in photos]})
        for _ in range(int(IMAGE_WAIT_SEC / _POLL_SEC)):
            if (_read(client).get("images") or 0) >= len(photos):
                break
            pause(_POLL_SEC)
        else:
            raise PublishNotAttempted("the photographs did not finish uploading", retryable=True)
    _submit(client, craigslist.DONE_WITH_IMAGES, "done with images")


_STEPS = {
    "subarea": _subarea,
    "hood": _hood,
    "type": _type,
    "cat": _cat,
    "edit": _edit,
    "geoverify": _geoverify,
    "editimage": _editimage,
}


def _check_preview(client, item: dict) -> None:
    """Refuse unless the preview shows the item's title and price; nothing is published yet."""
    text = str((client.evaluate(craigslist.PREVIEW_TEXT_JS) or {}).get("text") or "")
    title = (item.get("title") or "").strip()
    shown = re.search(re.escape(title) + r" - \$([\d,.]+)", text) if title else None
    if shown is None or not formfill.price_matches(shown.group(1), item.get("list_price")):
        raise PublishNotAttempted(f"the preview does not show {title!r} at its price")


def _commit(client, pause) -> PublishOutcome:
    try:
        marked = (client.evaluate(craigslist.PUBLISH_MARK_JS) or {}).get("marked")
        client.pace(craigslist.POST_URL)
    except page_governor.PagesSpent:
        raise
    except BrowserError as exc:
        raise PublishNotAttempted(f"nothing was published: {exc}", retryable=True) from exc
    if not marked:
        raise PublishNotAttempted("the preview offered no publish button", retryable=True)
    try:
        client.click(craigslist.PUBLISH, "publish")
    except ControlMoved as exc:
        raise PublishNotAttempted(f"nothing was published: {exc}", retryable=True) from exc
    except BrowserError as exc:
        raise PublishUnverified(f"the post may have gone up: {exc}") from exc
    try:
        try:
            _wait_past(client, "preview", pause)
        except PublishNotAttempted:
            return _unverified("Craigslist did not confirm the post")
        if _read(client).get("step") != "confirmed":
            return _unverified("Craigslist did not confirm the post; it may want an email click")
        manage = (client.evaluate(craigslist.MANAGE_LINK_JS) or {}).get("url")
        if not manage:
            return _unverified("the confirmation page named no manage link")
        client.navigate(manage)
        found = client.evaluate(craigslist.MANAGED_POST_JS) or {}
        url = str(found.get("url") or "")
        if not url or not marketplaces.is_canonical_listing_url(marketplaces.CRAIGSLIST, url):
            return _unverified(f"the manage page named no post address sellee can match: {url!r}")
        return PublishOutcome(listing_id=found.get("post_id"), url=url, verified=True)
    except BrowserError as exc:
        raise PublishUnverified(f"the post may have gone up: {exc}") from exc


def _unverified(reason: str) -> PublishOutcome:
    return PublishOutcome(listing_id=None, url="", verified=False, reason=reason)
