---
description: Publishing an item to Craigslist in the browser — the posting wizard, stage by stage
---

# Listing flow — Craigslist (browser)

Publishing one already-confirmed item to Craigslist by walking its real posting wizard in the
seller's own logged-in Chrome. The numbers were agreed with the seller before this pass started:
publish what the item record says, and change nothing.

Read the item with `get_item` for its title, price, description and condition. Its photos are in
your working directory, named in your prompt — upload those, not the paths `get_item` reports.

**Craigslist is unlike the other marketplaces this agent posts to, in three ways that change how you
work.** Read these before the steps.

1. **Nothing past the first page is addressable.** Every step POSTs to a one-time
   `action="/k/<token>/<suffix>"` URL and carries a fresh hidden `cryptedStepCheck`. You cannot
   navigate to a later step, go back, or reload — doing so invalidates the step. **Read the page you
   are on, every time**, and advance only through its own form.
2. **The steps are conditional.** The wizard's own stage list is
   `area → copyfromanother → subarea → hood → type → cat → edit → geoverify → editimage → preview
   → finalize`, and four of those appear only sometimes. Do not count steps or assume an order
   beyond what the page in front of you asks for.
3. **A wrong post cannot be fixed.** Craigslist does not let you move a posting to another site, and
   re-posting the same item inside 48 hours is a rule violation it answers by hiding your listings.
   So a mistake here is not a retry — it is a burned listing and a risked account. When in doubt,
   **stop and report** rather than pressing on.

## Before you touch the page

**Open your own tab first** (`browser_tabs`, action `new`) and work only in it. Other tabs may be
mid-flow for something else; never switch to one.

Load the remembered selectors for this flow in one call — `ui_cache_get` with market `craigslist`
and flow `listing` — and keep the map for the whole pass. An entry marked stale counts as absent.

**Snapshots are the expensive thing here.** One `browser_snapshot` per wizard *stage*, never one per
field. To check a single fact — a field's value, which radio is selected — use a scoped
`browser_evaluate` read, not a new snapshot.

## Steps

1. **Go to the posting page.** `browser_navigate` to the composer URL your prompt gives you. It
   looks like `https://post.craigslist.org/c/<area>`. **Never type a Craigslist URL from memory**;
   if the prompt has none, stop and report that — it means the seller's city is not configured, and
   guessing one posts their item where nobody local will see it.

2. **Confirm you are in the right city, before anything else.** The page names it in its title and
   heading, e.g. "SF bay area | choose nearest area". Two failure modes to catch here:
   - The page asks you to **"choose area"** from a long list of cities — that means the configured
     area code was not recognised. **Stop and report.** Picking from that list would publish the
     seller's item in a city chosen by you.
   - The page names a *different* city than the one your prompt's URL asked for. **Stop and report.**

3. **Decline any offer to copy a previous posting.** A seller with earlier posts may be shown a
   "copy from another posting" step first. Accepting it would republish the wrong item. Choose the
   option that starts a new posting.

4. **Nearest area, if asked.** Big cities ask for a subarea before anything else — the Bay Area
   offers city of san francisco, south bay, east bay, peninsula, north bay / marin, santa cruz.
   Pick the one nearest the item's location. If the item record gives you nothing to choose on,
   **ask rather than guess**: this decides which buyers ever see the listing. A neighbourhood step
   may follow; treat it the same way.

5. **Type of posting: "for sale by owner".** Match that label exactly. **"for sale by dealer" sits
   directly beneath it** and is both the wrong type and a paid one. Never choose any "wanted",
   "service", "job", "gig", "housing", "community" or "event" option.

6. **Category: the closest for-sale-by-owner category, or "general for sale - by owner".**
   **Choose exactly one** — in a paid section, each additional category is charged again.
   **Refuse these three outright** and report the item as unpublishable here; they cost the seller
   $5 in the US:
   - cars & trucks - by owner
   - motorcycles/scooters - by owner
   - rvs - by owner

   Also refuse anything labelled "- by dealer".

7. **Fill the posting form.** Read the page for its actual fields — they vary by category — and fill
   with `browser_fill_form` in as few calls as the page allows. That call is real typed input and is
   the right way to fill these. **Never set a field's value through `browser_evaluate`**: that is
   synthetic input with no focus or keystroke cadence behind it, which is exactly the automation
   signature this whole approach exists to avoid.
   - **Posting title** — capped at 70 characters. Shorten the item's title deliberately if it is
     longer; a title the form silently truncates reads as careless to buyers.
   - **Price** — the item's price, digits only, no currency symbol or separators.
   - **Postal code** — required, and the map step depends on it. If you have none, stop and report.
   - **Posting body** — the item's description.
   - **Condition**, where offered — read the dropdown's real options and pick the closest match to
     the item's condition. Where two fit, choose the **more** worn one: overstating condition is a
     claim made to a buyer on the seller's behalf.
   - Any other attribute the category asks for (make, model, size, dimensions) — fill it from the
     item record only. Leave blank anything the record does not say.

8. **Contact info: mail relay only.** This is how the agent hears from buyers at all, so it is not
   a preference:
   - keep the **craigslist mail relay** email option (it is the default, and required for free
     posts);
   - leave **"show my phone number" unchecked**;
   - leave **CL Chat unchecked** — buyers who use it reach a channel the agent cannot read, and they
     would get silence.

9. **Verify every field you filled, in ONE `browser_evaluate`** that returns all of them — never one
   read per field. Confirm each is what you sent (compare price on its digits — the page may
   reformat it). A field that did not take gets re-filled individually; a selector that resolved to
   the wrong thing gets `ui_cache_invalidate`d, re-found by looking at the page, and
   `ui_cache_record`ed once it works.

10. **The map step.** Confirm the map shows the right postal code area and continue. You do not need
    to place a pin or enter an address — the postal code is enough, and a street address would
    publish where the seller lives.

11. **Images.** One `browser_file_upload` with every file from your working directory — never one
    file per call. The uploader needs its "add images" control pressed first to open the file
    chooser. The first image is the one buyers see in listings. Finish that stage with its own
    button, which says **"done with images"**, not "continue".

12. **Read the preview back, then publish.** Check the title, price and body on the preview against
    the item one last time — this is the last moment a mistake is free. Then press **"publish"** as
    an ordinary click.

13. **Get the live URL from the seller's account page, then record it.** Go to the account page and
    read the link off the row for the posting you just made. **Only ever report a URL you read off
    a page** — never one you assembled. Then call `record_published_listing_url`: until you do,
    nobody who messages about this listing can be answered, so the publish is not finished.

    **Take it from the account page even if the confirmation page showed you a URL**, and this is
    not a preference. Craigslist serves the same posting under two addresses — an older
    `sfbay.craigslist.org/…/<numeric id>.html` and the canonical
    `www.craigslist.org/view/d/<slug>/<token>` it redirects to — and the id in one is not the id in
    the other. The account page links the canonical form, which is what everything else here
    matches a buyer's message against. Record the older form and the same posting is later read as
    a *different* listing the seller has never had managed, and adopted a second time.

    A Craigslist posting is **not always live the moment you publish it.** Two things may intervene:
    - craigslist may email the seller a confirmation link they must click before the post appears;
    - it may ask them to verify a phone number.

    Both are the seller's to do, not yours. If either appears, **record what you can and report that
    the posting is waiting on the seller** — naming which of the two it is. Do not wait, and do not
    start again.

14. **Close your tab.** `browser_tabs`, action `close`, once you are done — including when the
    publish failed. A tab left behind outlives this pass, and the agent's own reads can end up
    driving it.

## Never spend money

Posting a used item by owner is free in the categories this flow uses. Money can only enter in ways
you can see, and each one is a stop:

- Before clicking any control that might cost, classify it: free, paid, or unclear. **Unclear means
  click nothing.**
- **Stop the moment a page shows a fee, a total, a card form, or asks to confirm a purchase.** A
  Craigslist posting cannot go live unpaid, so stopping there costs nothing and charges nothing.
  Report that the category needs payment.
- Refuse the three paid by-owner vehicle categories at step 6, before the form is ever filled.
- Never enable an **auto-repost** option and never save a card: auto-repost charges again every time
  the posting expires, until someone turns it off by hand.

## When it goes wrong

- **A captcha, a phone verification, or a login wall:** stop this marketplace, escalate to the
  seller so they can complete it themselves, and **do not retry**. Repeated attempts against a wall
  are the clearest automation signal there is, and craigslist answers it by hiding the account's
  listings without saying so.
- **"This posting is being blocked" or any message about posting too often:** stop and report it.
  Do not rephrase the listing and try again — varying the text to get past a duplicate check is
  circumvention, and it escalates a warning into a blocked account.
- **A step you do not recognise:** report what the page says rather than guessing your way through
  it. An unfamiliar step is more likely a changed wizard than a step to improvise past.
- **A field needs re-finding more than three times in one pass:** stop and report. Something has
  changed structurally, and the selectors you did re-find are already recorded for next time.
- **Anything you cannot verify:** report it as failed. Reporting a listing as live when it is not
  costs the seller a sale; on craigslist it also costs a 48-hour wait before the item can be posted
  again.
