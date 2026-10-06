"""Posts an item to Craigslist by driving its posting form one `?s=` step at a time, under
`publisher.publish`'s contract: not attempted before the publish click, unverified after it."""

from __future__ import annotations

import logging
import re

from sellee import marketplaces
from sellee.browser import craigslist_account, editor, formfill, publisher
from sellee.browser import governor as page_governor
from sellee.browser.client import BrowserError, ControlMoved
from sellee.browser.markets import craigslist
from sellee.browser.publisher import (
    PublishNeedsSeller,
    PublishNotAttempted,
    PublishOutcome,
    PublishSignedOut,
    PublishUnverified,
)

log = logging.getLogger(__name__)

# A US posting is at most eight steps; past this the form is looping.
MAX_STEPS = 12
# How long a submitted step, or an image upload, gets to show its next page.
TRANSITION_WAIT_SEC = 20.0
IMAGE_WAIT_SEC = 60.0
_POLL_SEC = 0.5
_MENU_WAIT_SEC = 3.0

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
        step = page.get("step") or ""
        log.info("Craigslist posting step %r", step)
        if step == "terms":
            # Live, Craigslist put its terms of use in front of a post; sellee always accepts.
            _accept_terms(client, pause)
            continue
        if not page.get("logged_in"):
            raise PublishSignedOut("Craigslist shows the account signed out", market="Craigslist")
        site = str(page.get("site") or "")
        if site.lower() != craigslist.SITE_NAME.lower():
            raise PublishNotAttempted(
                f"Craigslist put the post on {site!r}, not {craigslist.SITE_NAME}"
            )
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
    refusal = ""
    try:
        refusal = (client.evaluate(craigslist.REFUSAL_JS) or {}).get("text") or ""
    except BrowserError:
        pass
    reason = f"Craigslist did not move past {step!r}" + (f": {refusal}" if refusal else "")
    log.warning("%s", reason)
    raise PublishNotAttempted(reason, retryable=True)


def _accept_terms(client, pause) -> str:
    if not (client.evaluate(craigslist.TERMS_MARK_JS) or {}).get("marked"):
        raise PublishNotAttempted("the terms page offered no accept button", retryable=True)
    _submit(client, craigslist.TERMS, "ACCEPT the terms of use")
    return _wait_past(client, "terms", pause)


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
            options=tuple(options),
        )
    _submit(client, craigslist.CONTINUE, "continue")


def _hood(client, page, item, photos, seller, pause) -> None:
    if not (client.evaluate(craigslist.HOOD_BYPASS_JS) or {}).get("chosen"):
        raise PublishNotAttempted("the neighborhood step offered no way past it")
    client.click(craigslist.CHOICE, "bypass this step")
    _submit(client, craigslist.CONTINUE, "continue")


def _copyfromanother(client, page, item, photos, seller, pause) -> None:
    """Never copy an earlier post: that would republish another item."""
    answer = client.evaluate(craigslist.NEW_POSTING_JS) or {}
    if not answer.get("chosen"):
        raise PublishNotAttempted(
            f"no single 'start a new posting' choice among {answer.get('options') or []}"
        )
    if answer.get("radio"):
        client.click(craigslist.CHOICE, "start a new posting")
        _submit(client, craigslist.CONTINUE, "continue")
    else:
        _submit(client, craigslist.CHOICE, "start a new posting")


def _type(client, page, item, photos, seller, pause) -> None:
    _pick(client, craigslist.FOR_SALE_BY_OWNER)


def _cat(client, page, item, photos, seller, pause) -> None:
    _pick(client, item.get(craigslist.CATEGORY_KEY) or craigslist.DEFAULT_CATEGORY)


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
    _set_condition(client, item, pause)
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


def _set_condition(client, item: dict, pause) -> None:
    """Best-effort, as the condition is optional: open its menu widget and pick the item."""
    wanted = craigslist.condition_for(str(item.get("condition") or ""))
    if not wanted:
        return
    try:
        if not (client.evaluate(craigslist.CONDITION_OPEN_MARK_JS) or {}).get("marked"):
            log.warning("the Craigslist form has no condition menu")
            return
        client.click(craigslist.CONDITION_OPEN, "the condition")
        found: dict = {}
        for _ in range(int(_MENU_WAIT_SEC / _POLL_SEC)):
            found = client.evaluate(craigslist.condition_item_js(wanted)) or {}
            if found.get("marked"):
                break
            pause(_POLL_SEC)
        if not found.get("marked"):
            log.warning("the condition menu offered %r, not %r", found.get("options"), wanted)
            return
        client.click(craigslist.CONDITION_ITEM, wanted)
        shown = (client.evaluate(craigslist.CONDITION_READ_JS) or {}).get("shown")
        if shown != wanted:
            log.warning("the Craigslist condition shows %r, not %r", shown, wanted)
    except BrowserError:
        log.warning("could not set the Craigslist condition", exc_info=True)


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
    "copyfromanother": _copyfromanother,
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


def _press_publish(client) -> None:
    """Press the preview's publish: not attempted before the press, unverified from it on."""
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


def _commit(client, pause) -> PublishOutcome:
    _press_publish(client)
    try:
        try:
            after = _wait_past(client, "preview", pause)
            if after == "terms":
                after = _accept_terms(client, pause)
        except PublishNotAttempted:
            return _unverified("Craigslist did not confirm the post")
        log.info("Craigslist publish led to %r", after)
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


# --- editing a live post -------------------------------------------------------------------------

# The edit form arrives filled, so a changed box is emptied before it is typed into.
_EDIT_BOXES = {
    "title": craigslist.TITLE,
    "price": craigslist.PRICE,
    "description": craigslist.BODY,
}


class _Touched(Exception):
    """Raised from a step that may already have changed the live post, such as an image removed."""


def revise(client, item: dict, *, listing_url: str, changed, sleep=None):
    """Change a live post to what the item says, from its manage page. Under `editor.revise`'s
    contract: not attempted until something may have changed the post, unverified from then on."""
    pause = sleep or formfill.sleep
    if not marketplaces.is_canonical_listing_url(marketplaces.CRAIGSLIST, listing_url):
        raise editor.ReviseNotAttempted(f"{listing_url!r} is not a Craigslist post address")
    wanted = set(changed)
    if not wanted:
        raise editor.ReviseNotAttempted("nothing to change")
    manage = craigslist.manage_url(listing_url)
    fields = wanted - {"photos"}
    halves = []
    if fields:
        halves.append((craigslist.EDIT_TEXT, fields, None))
    if "photos" in wanted:
        halves.append((craigslist.EDIT_IMAGES, set(), item.get("photos") or []))
    saved = False
    outcomes = []
    try:
        for entry, text, photos in halves:
            outcomes.append(
                _revise_once(client, item, listing_url, manage, entry, pause, text, photos)
            )
            saved = True
    except editor.ReviseNotAttempted as exc:
        if saved:
            # An earlier half is live, so "nothing was saved" is false; a retry repeats both.
            raise editor.ReviseUnverified(f"only part of the edit was made: {exc}") from exc
        raise
    return _merged(outcomes)


def _revise_once(client, item, listing_url: str, manage: str, entry: str, pause, text, photos):
    staged = _stage_all(item, photos) if photos is not None else None
    try:
        client.navigate_visible(manage)
        if not _read(client).get("logged_in"):
            raise editor.ReviseSignedOut("Craigslist shows the account signed out")
        _submit(client, entry, "the manage page's edit control")
        _wait_past(client, "", pause)
        _walk_edit(client, item, text, staged, pause)
        _press_publish(client)
    except _Touched as exc:
        raise editor.ReviseUnverified(f"the post may have changed: {exc}") from exc
    except PublishNotAttempted as exc:
        raise editor.ReviseNotAttempted(str(exc), retryable=exc.retryable) from exc
    except PublishUnverified as exc:
        raise editor.ReviseUnverified(str(exc)) from exc
    except (editor.ReviseNotAttempted, editor.ReviseUnverified):
        raise
    except BrowserError as exc:
        raise editor.ReviseNotAttempted(f"could not edit the post: {exc}", retryable=True) from exc
    finally:
        if staged is not None:
            publisher.clear_staged(item["id"])
    expected = len(staged) if staged is not None else None
    return _confirm_revision(client, item, listing_url, manage, text, expected, pause)


def _stage_all(item: dict, photos) -> list:
    """Every one of the item's photographs, or none: removing the post's images for a partial set
    would publish a post missing some of them."""
    staged = publisher.stage_photos(item["id"], photos)
    if len(staged) != len(photos):
        publisher.clear_staged(item["id"])
        raise editor.ReviseNotAttempted(
            f"only {len(staged)} of {len(photos)} photographs could be read", retryable=True
        )
    return staged


def _walk_edit(client, item: dict, text, photos, pause) -> None:
    for _ in range(MAX_STEPS):
        step = _read(client).get("step") or ""
        log.info("Craigslist edit step %r", step)
        if step == "preview":
            _check_edit_preview(client, item, text)
            return
        if step == "edit":
            _replace_text(client, item, text, pause)
        elif step == "editimage":
            _replace_photos(client, photos, pause)
        elif step == "geoverify":
            _submit(client, craigslist.MAP_CONTINUE, "continue")
        else:
            raise PublishNotAttempted(
                f"Craigslist showed an edit page sellee does not know: {step!r}"
            )
        _wait_past(client, step, pause)
    raise PublishNotAttempted("the edit never reached its preview", retryable=True)


def _check_edit_preview(client, item: dict, text) -> None:
    """Refuse unless the preview shows each changed field; the rest is the post as it stood."""
    shown = str((client.evaluate(craigslist.PREVIEW_TEXT_JS) or {}).get("text") or "")
    wrong = _still_old(item, text, shown)
    if wrong:
        prices = [
            shown[max(0, m.start() - 60) : m.end() + 20] for m in re.finditer(r"\$[\d,.]+", shown)
        ]
        log.info("Craigslist edit preview shows prices %r", prices[:4])
        raise PublishNotAttempted(f"the preview does not show the new {', '.join(wrong)}")


# "<title> - $<price>", mid-line and sometimes with no "(<area>)" after it.
_PRICE = re.compile(r" - \$(\d[\d,]*(?:\.\d+)?)(?![\d,])")


def _shows_title(item: dict, shown: str) -> bool:
    title = (item.get("title") or "").strip()
    return bool(title) and re.search(re.escape(title) + r" - \$", shown) is not None


def _still_old(item: dict, text, shown: str) -> list:
    """Which changed fields `shown`, a post's page text, does not carry."""
    price = _PRICE.search(shown)
    wrong = []
    if "title" in text and not _shows_title(item, shown):
        wrong.append("title")
    if "list_price" in text and (
        price is None or not formfill.price_matches(price.group(1), item.get("list_price"))
    ):
        wrong.append("list_price")
    if "description" in text and _norm(item.get("description")) not in _norm(shown):
        wrong.append("description")
    return wrong


def _replace_text(client, item: dict, text, pause) -> None:
    fields = [
        (step, value)
        for step, name, value in (
            ("title", "title", item.get("title") or ""),
            ("price", "list_price", formfill.bare_price(item.get("list_price"))),
            ("description", "description", item.get("description") or ""),
        )
        if name in text
    ]
    formfill.type_fields(
        client,
        _EDIT_BOXES.__getitem__,
        fields,
        list(_EDIT_BOXES),
        lambda message: PublishNotAttempted(message, retryable=True),
        pause,
        replace=True,
    )
    # Live, the scroll to "continue" stepped a still-focused number box from 18 down to 12.
    client.call_tool("browser_press_key", {"key": "Tab"})
    seen = client.evaluate(craigslist.EDIT_READBACK_JS) or {}
    if "title" in text and _norm(seen.get("title")) != _norm(item.get("title")):
        raise PublishNotAttempted(f"the edit form shows the title as {seen.get('title')!r}")
    if "list_price" in text and not formfill.price_matches(
        seen.get("price"), item.get("list_price")
    ):
        raise PublishNotAttempted(f"the edit form shows the price as {seen.get('price')!r}")
    if "description" in text and _norm(seen.get("description")) != _norm(item.get("description")):
        raise PublishNotAttempted("the edit form's description is not what was asked for")
    _submit(client, craigslist.CONTINUE, "continue")


def _replace_photos(client, photos, pause) -> None:
    """Remove every image the post has, then add the item's, and see the count is exactly theirs.
    Once one image is gone the live post may have lost it, so failures from there are `_Touched`."""
    if not photos:
        raise PublishNotAttempted("the item has no photographs to put on the post")
    removed = 0
    for _ in range(int(IMAGE_WAIT_SEC / _POLL_SEC)):
        if not (_read(client).get("images") or 0):
            break
        if (client.evaluate(craigslist.DELETE_IMAGE_MARK_JS) or {}).get("marked"):
            client.click(craigslist.DELETE_IMAGE, "remove image")
            removed += 1
        pause(_POLL_SEC)
    else:
        failure = "the old photographs would not all come off"
        if removed:
            raise _Touched(failure)
        raise PublishNotAttempted(failure, retryable=True)
    try:
        _editimage(client, {}, {}, photos, {}, pause)
    except (PublishNotAttempted, BrowserError) as exc:
        if removed:
            raise _Touched(str(exc)) from exc
        raise


def _confirm_revision(
    client, item: dict, listing_url: str, manage: str, text, photos: int | None, pause
) -> editor.ReviseOutcome:
    """Read the manage page, which shows the post as it now stands, for each changed field; for
    photographs, the image count a fresh "Edit Images" starts from, left without saving."""
    try:
        client.navigate(manage)
        found = client.evaluate(craigslist.MANAGED_POST_JS) or {}
        if found.get("url") != listing_url:
            raise editor.ReviseUnverified(
                f"the manage page names another post: {found.get('url')!r}"
            )
        shown = str(found.get("text") or "")
        images = _saved_images(client, pause) if photos is not None else None
    except editor.ReviseUnverified:
        raise
    except BrowserError as exc:
        raise editor.ReviseUnverified(f"saved, but the post could not be read back: {exc}") from exc
    price = _PRICE.search(shown)
    accepted = {"price": price.group(1)} if price else {}
    if _shows_title(item, shown):
        accepted["title"] = (item.get("title") or "").strip()
    mismatched = _still_old(item, text, shown)
    if "description" in text and "description" not in mismatched:
        accepted["description"] = item.get("description")
    if photos is not None and images != photos:
        mismatched.append("photos")
    return editor.ReviseOutcome(
        verified=not mismatched, accepted=accepted, mismatched=tuple(mismatched)
    )


def _saved_images(client, pause) -> int | None:
    _submit(client, craigslist.EDIT_IMAGES, "Edit Images")
    try:
        _wait_past(client, "", pause)
    except PublishNotAttempted:
        return None
    page = _read(client)
    return page.get("images") if page.get("step") == "editimage" else None


def _merged(outcomes: list) -> editor.ReviseOutcome:
    mismatched = tuple(name for outcome in outcomes for name in outcome.mismatched)
    accepted: dict = {}
    for outcome in outcomes:
        accepted.update(outcome.accepted)
    return editor.ReviseOutcome(
        verified=not mismatched,
        accepted=accepted,
        mismatched=mismatched,
        reason=f"it still shows the old {', '.join(mismatched)}" if mismatched else "",
    )


def _norm(text) -> str:
    return " ".join(str(text or "").split())
