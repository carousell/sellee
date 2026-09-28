---
description: Changing a listing that is already live — price, title, description, photos, everywhere
---

# Changing a live listing

When the seller wants something different on a listing that is already up — a lower price, a line
added to the description, a better title, new photos — that is `update_live_listing`, one call. It
changes the item and every marketplace the item is listed on.

**Never `update_item` on something already listed.** It changes the record here and nothing a
buyer can see, so "done" after it would be untrue on every marketplace.

## Confirm first

One message: what it says now, what it will say, and where it changes — the same discipline as
confirming a new listing (listing flow, step 3). A price the market won't support gets the
**Anomaly** ask from seller-comms before this one; comps are the listing flow's step 2. What a
description may say is the listing flow's section 5 — an edit is no licence to add an address or a
meetup promise.

`options: ["✅ Change it", "✏️ Something else"]`. A tap on **✅ Change it** is the go-ahead.

Say these in the same message when they apply, because they change the decision:

- **A buyer already offered more** than the new price (`negotiate_status` shows the standing
  offers). Name the buyer and their offer; lowering under it gives money away.
- **A deal is in flight** — the item is being bid on or is reserved. The tool refuses a price change
  then, because it would move the price underneath that buyer. Say so plainly, and that releasing
  the deal (`negotiate_release`) or letting it finish is the way through.

## Say what actually changed

The result has one status per marketplace. Report each one as it is, never one "done":

- `updated` — carousell.ai shows it now.
- `queued` — a browser marketplace is being changed in the background. Say it has started and that
  they'll get a message when it lands. **Never say it has changed.** The message comes on its own.
- `not_connected` / `manual` — it will not change by itself. Give them the link and say what to
  change there.
- `failed` — say what failed, in the reason's own words; asking you again is how they retry.

**`floor_lowered`** means their private floor moved down to the new price — a floor above what they
are asking can't be what they meant. Tell them it moved, never either number (the floor's rules
live in seller-comms, under "No floor yet").

A checkout link already sent stays good at the price it was sent at. Nothing takes a link back, so
don't imply the edit did.

## This is not a relist

There is no taking a listing down and posting it again. The live listing is what every buyer
conversation about it is attached to; replacing it would cut those buyers off and leave a second
copy besides. If the seller wants a fresh start, the edit *is* the fresh start: new price, new
words, new photos, same listing. A genuinely different thing is a new item.

Currency and condition can't be changed on a live listing. If they need to be, that is a new item.
