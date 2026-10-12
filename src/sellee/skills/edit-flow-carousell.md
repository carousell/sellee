---
description: Changing a live Carousell listing in the browser — the edit form, step by step
---

# Edit flow — Carousell (browser)

Changing one live Carousell listing so it matches the item record, in the seller's own logged-in
Chrome. The change was confirmed with the seller before this pass started: make the listing say
what the item says, for the fields your prompt names, and touch nothing else.

Read the item with `get_item` for the new values. If photos are among the fields, the new set is in
your working directory, named in your prompt, in order — upload those, not the paths `get_item`
reports.

## Before you touch the page

Load the remembered selectors in one call — `ui_cache_get` with market `carousell` and flow `edit`
— and keep the map for the whole pass. An entry marked stale counts as absent.

**Snapshots are the expensive thing here.** One `browser_snapshot` per form *step*, never one per
field, and none where the remembered selectors resolve. To check a single fact, use a scoped
`browser_evaluate` read.

**Open your own tab first** (`browser_tabs`, action `new`) and work only in it.

## Steps

1. **Go to the listing.** `browser_navigate` to the listing URL your prompt gives you. Never type a
   Carousell URL from memory. If the page says the listing is gone or sold, stop: record `failed`
   with that reason.
2. **Open its edit form** with the listing page's own **"Edit Listing"** link — click the link
   rather than typing where it points. A page with no such link is not this seller's listing, or
   no longer up — stop and record `failed`.
3. **Change only the named fields.** The form's inputs carry stable `name` attributes — reach them
   by those, not by class: `field_title`, `field_description`, and `field_price` (a number box
   beside the currency label). For each: clear what is there, then type the new value with
   `browser_fill_form` or `browser_type` — real typed input, never a value set through
   `browser_evaluate`. Type a price as a bare number: `120`, never `S$120` or `1,299`.
4. **Photos, if named:** remove every existing photo with its **"Delete image"** control, then add
   all the new ones in **one** `browser_file_upload` from **"Select photos"**, in the order given —
   the first is the cover. The overlays that swallow an ordinary click here are clicked through
   `browser_evaluate`.
5. **Read the form back in ONE `browser_evaluate`** returning every field you changed. Compare a
   price as a number — `115`, `115.00` and `S$115` are all the same price. A field that did not
   take gets re-filled; a selector that resolved to the wrong thing gets `ui_cache_invalidate`d,
   re-found, and `ui_cache_record`ed once it works.
6. **Meet-up stays OFF.** If the form shows the meet-up deal method, confirm it is still off before
   saving — Carousell pre-fills the seller's home street into it.
7. **Save** with the form's **"Update"** button, as an ordinary click. Never through
   `browser_evaluate`.
8. **Verify on the listing page itself.** Navigate back to the listing URL and read the page's
   `application/ld+json` `Product` block in one `browser_evaluate` — its `name`, `offers.price` and
   `description` are the page's own record of what it shows. `offers.price` reads like `"115.00"`:
   compare it as a number.
9. **Record it.** `record_listing_revision` with `done` only if every changed field reads back as
   the item says, putting what the page shows in `shown`. Anything else is `failed`, with the
   reason — the seller is told either way.
10. **Close your tab**, including when it failed.

## Never spend money

Never click anything that costs coins or money: no "Promote", no "Spotlight", no paid bump. Unclear
means click nothing. If a payment or top-up screen appears, dismiss it and record `failed`.

## When it goes wrong

- **Logged out, a checkpoint, or a captcha:** stop and record `failed` with that reason. Do not
  retry against a login wall.
- **A field needs re-finding more than three times:** stop and record `failed`.
- **Anything you cannot verify:** record it as `failed`. Saying a price changed when it did not
  costs the seller a buyer who sees the old one.
