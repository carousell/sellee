"""Clauses the rail refuses with, that more than one tool has to recognise.

Only the clause lives here, not the wording around it: each surface says something different
about what is blocked, and each tier a different thing about who to tell. What must not be
duplicated is the substring — it is matched against backend copy, and a second copy would be a
second thing to miss when that copy drifts.
"""

from __future__ import annotations

# The rail ships a bare refusal with no error code, so the message is the only signal. If this
# clause drifts out of the backend copy the mapping stops firing and the raw text surfaces —
# degraded (it still says what is wrong), never wrong.
NO_MARKET_CLAUSE = "seller has no market"
