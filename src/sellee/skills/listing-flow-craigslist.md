---
description: Publishing an item to Craigslist in the browser — the posting form, step by step
---

# Listing flow — Craigslist (browser)

Publishing one already-confirmed item to Craigslist from the seller's own logged-in account in
Chrome. The numbers were agreed with the seller before this pass started: publish what the item
record says, and change nothing.

Read the item with `get_item` for its title, price, description and condition. Its photos are in
your working directory, named in your prompt — upload those, not the paths `get_item` reports. Your
prompt also gives the seller's ZIP code; it is the only location you use.

## Before you touch the page

**Open your own tab first** (`browser_tabs`, action `new`) and work only in it. Other tabs may be
mid-flow for something else; never switch to one.

Craigslist's posting form is a series of pages on one address, `post.craigslist.org/k/…`, and the
page you are on is named in its `?s=` query: `subarea`, `hood`, `type`, `cat`, `edit`, `geoverify`,
`editimage`, `preview`. Read `location.href` with `browser_evaluate` to know where you are. Use one
`browser_snapshot` per page, never one per field.

After every "continue" or "publish", wait for `?s=` to change before acting; the click returns
before the next page loads. If it has not changed after a few seconds, check once for an error on
the page, then click again at most once.

## Steps

1. **Start a post.** `browser_navigate` to `https://post.craigslist.org/c/`. Craigslist picks the
   site from where the browser is; its name is in the page header. If that site is not the one
   for the seller's ZIP code, follow the page's own link to change the location and choose the
   right one. Never type another Craigslist address.
2. **Area (`subarea`, on large sites only).** Choose the area that contains the seller's ZIP code.
3. **Neighborhood (`hood`, on large sites only).** Choose the neighborhood that contains the ZIP
   code if you can tell; otherwise choose "bypass this step".
4. **Type (`type`).** Choose "for sale by owner".
5. **Category (`cat`).** Choose the category that fits the item, matching its label exactly — the
   first radio's label on this page wraps the whole list. **If the right category's label carries a
   fee, such as `cars & trucks ($5)`, stop: this item cannot be posted without paying.** Report it as
   unpublishable on Craigslist and close your tab.
6. **Details (`edit`).** Fill posting title, price (digits only), ZIP code and description in ONE
   `browser_fill_form`. Fill "city or neighborhood" with the area you chose, if the field is shown.
   Set condition from the item's condition. Then:
   - **Uncheck "CL chat"** (`contact_chat_ok`) if it is checked. Buyers must reach the seller by
     email only, through Craigslist's mail relay.
   - Leave every phone and address box unchecked and empty. Never publish the seller's phone or
     street address.
   - Leave the reply email as Craigslist set it.

   Verify every field you filled, and that CL chat is off, in ONE `browser_evaluate`. Never set a
   field's value through `browser_evaluate`: that is synthetic input, the automation signature this
   approach exists to avoid.
7. **Map (`geoverify`, US sites).** The map is already placed from the ZIP code. Click "continue";
   do not type a street.
8. **Images (`editimage`).** One `browser_file_upload` with every file from your working
   directory into the page's file input, never one file per call. Wait until the page counts them
   ("this posting has N images"), then click "done with images".
9. **Preview (`preview`).** Read the title and price back from the preview in ONE
   `browser_evaluate` and compare the price on its digits. If either is wrong, use "edit post" and
   fix it. Then click "publish" as an ordinary click, never through `browser_evaluate`.
10. **Get the post's URL from the page, then record it.** The confirmation page says "Thanks for
    posting!" and links "Manage your post". Open that link: the manage page shows "This posting can
    be seen at" a `www.craigslist.org/view/d/…` address. Read that address off the page, as
    `https://www.craigslist.org/view/d/<slug>/<token>` with nothing after the token, and call
    `record_published_listing_url`. Not the `….craigslist.org/…/d/….html` address on the
    confirmation page: buyers' mail names the `/view/d/` one. Until it is recorded, nobody who
    writes about this post can be answered, so the publish is not finished. **Only ever report an
    address you read off the page.**
11. **Close your tab** (`browser_tabs`, action `close`), including when the publish failed.

## Never spend money

Never click anything that costs money: no paid category, no "repost" with a fee, no promoted
listing. If a payment or card screen appears at any point, stop, close your tab, and report that
the item needs money to post — do not enter anything.

## When it goes wrong

- **Logged out, a captcha, a phone-verification request, or a page saying the account or posting
  is blocked:** stop, close your tab, and report what the page said, word for word. Do not retry;
  repeated attempts against a wall are the clearest automation signal there is.
- **An email confirmation is asked for** ("check your email"): report it. Do not wait for it.
- **Anything you cannot verify:** report it as failed. Reporting a post as live when it is not
  costs the seller a sale.
