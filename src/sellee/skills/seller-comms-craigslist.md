---
description: Talking to a seller who works Craigslist — its ZIP and area questions, the account, and its buyers
---

# Seller comms on Craigslist

Posting to Craigslist asks the seller two things, each once and only after they chose Craigslist:
their US ZIP code, and on a big site like SF bay area which area they are in (the question lists
the areas). Their answer is yours to store: `update_seller_config` with `basics`, `zip` as five
digits or `craigslist_area` exactly as one of the offered names — "city of san francisco", not
"SF". The post goes ahead on its own once it is stored, so say that, not that it is live. Never
ask for either outside those questions. The area question also carries a button per area, and a
tap stores it with no help from you. The Craigslist category is not theirs to pick: sellee chooses
it, and if Craigslist takes a post down after a flag, posts it again in the next one and tells them.
Never ask them for a Craigslist category.

The seller created their Craigslist account themselves, in your Chrome window, with the
registration address; its mail goes to an inbox only you read, so you never sign in for them. A
seller may have connected their own Craigslist account instead, and was told then that its buyers
write to their own inbox: you post and edit there, but never see or answer a Craigslist buyer, so
never say you will. When
it is signed out, the account notice asks them to sign back in at their computer
(`sellee connect craigslist`, or Sign in on desktop). To sign in from their own browser they tap
Sign in myself on the account-ready notice, and sellee sends them the login link; never invent
another way in. A sold item's post is closed from the Open on desktop button, which opens it in
your Chrome; if that failed, point them at the button on the latest notice.

Nor can they write to a Craigslist buyer: Craigslist chat is off, and the buyer's address only takes
mail from the account's own. Never point them at Craigslist chat or the buyer's email; offer to
send what they want said, and pass it on with `send_reply` exactly as they wrote it.
