"""Answering buyers who arrive as email rather than as an on-site conversation.

Craigslist has no messaging: a buyer's only route to a seller is an anonymised relay address, and
the seller's own mailbox is where those messages land. So this is a second implementation of the
seams the browser layer fills — which conversations exist, what one holds, and how a reply is
committed — for a market that has no page to read them on.

It is a **transport, not a marketplace**. A thread here carries `market="craigslist"`, because
`tools/reply.py` refuses to send unless the thread's market is one the seller connected, and a
mailbox is not something anyone connects as a marketplace. Everything above the seams is reused
untouched: `reconcile` for tail alignment and content-derived message ids, the threads store, the
reply lane and its coalesced pass, the intent lifecycle, and the offline scam pre-scan.
"""
