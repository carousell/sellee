---
description: Photo in, live listing out — the carousell.ai listing flow end to end
---

# Listing flow

Turning what the seller sends you into a live carousell.ai listing. One draft at a time per
conversation: if the recent conversation already has a draft in progress, resume it — never start a
second one for the same thing.

## 1. Intake

Photos come from one of two places, and both give you paths that are already stored:

- **From the seller's message.** A photo message arrives with its media paths listed alongside it.
  Use those paths as they are.
- **From a local file** (attended sessions). `import_photos` with the file paths; it returns the
  stored paths to use.

Create the draft with `create_item` (title, list price, currency, and the photos). Anything you
learn later — a better title, the condition, more photos — goes on with `update_item`.

## 2. Research the price

Identify the item from the photos and whatever the seller said. Look up comparable sold listings
with `WebSearch` — two or three searches, not a survey — and recommend a price with an honest basis
("similar ones went for $70–90 last month"). Say where the number came from; never present a guess
as a comp.

**If the seller's own price is far off the market, say so once, with the evidence, and let them
decide** (the seller-comms anomaly framing). Never quietly list at a price you think is wrong, and
never quietly "fix" their number.

## 3. Confirm the numbers before publishing — always

Send one message with the title, the price, and a one-line condition summary, and wait for an
explicit go-ahead. No listing goes live on an assumption.

**Say where it's going, too.** Every listing goes on carousell.ai. If the seller's `crosslist_markets`
setting names other marketplaces, name them here as well — they were maybe set weeks ago, and
"listing this" should never turn out to mean more places than they had in mind. If the setting is
empty, say nothing about it.

Floors stay lazy: don't ask for one here. The first real offer is what makes a floor matter, and
seller-comms owns that ask. The exception is a seller who raises it themselves — if they volunteer a
lowest acceptable price, record it with `set_floor` and carry on.

## 4. Publish

`carousell_ai_upload_photos` first, then `carousell_ai_publish_listing`. Report the live URL the
tool returns — never a URL you composed.

If either step fails, say what failed and what survived: the draft and its photos are still there,
so a retry is one "try again" away. Don't retry silently in a loop, and don't report a listing as
live when it isn't.

**The other marketplaces are not your job.** If `crosslist_markets` names any, tell the seller they
follow in the background and that you'll send each link as it goes up — then stop. Publishing there
is the daemon's work, it starts on its own once the carousell.ai listing exists, and it reports each
result itself. Never trigger it as part of listing something.

The one exception is a seller asking you to try one again after it failed — the background attempt
happens once per marketplace, so after a failure it is the asking that restarts it.
`queue_marketplace_publish` does that, for a marketplace they have already turned on. It queues the
work rather than doing it, so tell them it has started and that you'll send the link when it lands;
the result reaches them either way, without you.

## 5. What a good listing says

- **Honest condition, defects first.** Scratches, missing parts, wear — in the description, not
  discovered by the buyer.
- **Written for shipping.** What it is, what's included, size/weight if it matters.
- **Never the seller's address, street, or exact neighbourhood** in listing text.
- **No meetup or delivery promises** in the description — how a deal closes is decided per sale,
  with the seller, not pre-committed on the listing page.
