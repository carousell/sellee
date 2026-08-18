---
description: Publishing an item to Carousell in the browser — the composer, step by step
---

# Listing flow — Carousell (browser)

Publishing one already-confirmed item to Carousell by filling its real composer in the seller's own
logged-in Chrome. The numbers were agreed with the seller before this pass started: publish what the
item record says, and change nothing.

Read the item with `get_item` for its title, price, description and condition. Its photos are in
your working directory, named in your prompt — upload those, not the paths `get_item` reports.

## Before you touch the page

Load the remembered selectors for this flow in one call — `ui_cache_get` with market `carousell` and
flow `listing` — and keep the map for the whole pass. An entry marked stale counts as absent.

**Snapshots are the expensive thing here.** One `browser_snapshot` per composer *step*, never one per
field, and none at all on a step where the remembered selectors resolve. To check a single fact — a
field's value, whether a toggle is off — use a scoped `browser_evaluate` read, not a new snapshot.

**Open your own tab first** (`browser_tabs`, action `new`) and work only in it. Other tabs may be
mid-flow for something else; never switch to one.

## Steps

1. **Go to the composer.** `browser_navigate` to the composer URL your prompt gives you. Never type
   a Carousell URL from memory; if the prompt has none, stop and report that.
2. **All photos in one upload.** One `browser_file_upload` with every file from your working
   directory — never one file per call, which is the slowest thing this flow can do. The "Select
   photos" dropzone and
   the "Save & close" dialog sit behind overlays that swallow an ordinary click; click those through
   `browser_evaluate`. If a restored draft from a previous attempt is showing, clear it first.
3. **Category.** Carousell usually detects one from the photos. Accept it when it fits the item;
   otherwise pick the closest.
4. **Fill title, price and description in ONE `browser_fill_form`.** That call is real typed input
   and is the right way to fill these three. Never set a field's value through `browser_evaluate`:
   that is synthetic input with no focus or keystroke cadence behind it, which is exactly the
   automation signature this whole approach exists to avoid.
5. **Verify every field you filled, in ONE `browser_evaluate`** that returns all three values —
   never one read per field. Confirm each is what you sent (compare price on its digits — the page
   may reformat it). A field that did not take gets re-filled individually; a selector that resolved
   to the wrong thing gets `ui_cache_invalidate`d, re-found by looking at the page, and
   `ui_cache_record`ed once it works.
6. **Condition.** Set it from the item's condition.
7. **Delivery on, meet-up OFF — on every listing, without exception.** Enable mail/courier delivery,
   then disable the meet-up deal method entirely and **read it back to confirm it is off**.
   Two separate reasons, both real: Carousell pre-fills the seller's saved home street address into
   meet-up, which would publish their address; and an enabled meet-up with no location makes "List
   now" fail silently with a validation error, so the pass looks stuck on a publish that never
   happens. Clearing the location is *not* enough — the method itself must be off. The toggle is a
   hidden checkbox: click its wrapping `<label>`, not the input.
8. **Read the suggested price** if the page shows one, and report it — it is the strongest signal of
   what the item actually sells for. Read it, never remember it: it only exists mid-flow.
9. **Publish.** Read the preview back, then click "List now" as an ordinary click. Never click it
   through `browser_evaluate`. If it appears to do nothing, the cause is almost always an unmet
   validation — check step 7 before clicking again.
10. **Get the live URL from the page, then record it.** Dismiss the post-publish tour and survey if
    they appear. The page does not reliably land on the listing, so read the permalink
    (`/p/<slug>-<id>/`) off the seller's own listings grid, matching the item you just posted. **Only
    ever report a URL you read off the page** — never one you assembled. Then call
    `record_published_listing_url`: until you do, nobody who messages about this listing can be
    answered, so the publish is not finished. No readable permalink means the publish failed: say so,
    rather than reporting a listing as live.
11. **Close your tab.** `browser_tabs`, action `close`, once the URL is recorded — including when
    the publish failed. A tab left behind outlives this pass, and the agent's own inbox reads can
    end up driving it: leaving the composer open is how a finished publish blinds the reply loop.

## Never spend money

Never click anything that costs coins or money: no "Promote", no "Spotlight", no paid bump. Before
clicking any control that might, classify it: free, paid, or unclear. **Unclear means click
nothing.** If a payment or top-up screen appears at any point, stop, dismiss it, and report that the
step needs money — do not confirm a purchase.

## When it goes wrong

- **Logged out, a checkpoint, or a captcha:** stop this marketplace, escalate to the seller to
  re-authenticate, and do not retry. Repeated attempts against a login wall are the clearest
  automation signal there is.
- **A field needs re-finding more than three times in one pass:** stop and report it. Something has
  changed structurally, and the selectors you did re-find are already recorded for next time.
- **Anything you cannot verify:** report it as failed. The draft and its photos survive, so a retry
  costs nothing — reporting a listing as live when it is not costs the seller a sale.
