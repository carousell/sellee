---
description: What happened while you were away, and what needs you
---

Call `get_catchup` and render the needs-me queue for the seller.

Lead with what needs a decision — open escalations, pending setting approvals — then what merely
happened. Keep each item to a line or two: what it is, which item or buyer it concerns, and the
decision being asked for. Don't dump the raw tool output.

`untold`, when present, is what was done on their listings that they had not heard about; it goes
with what merely happened, or with what needs a decision when `asks` is true, since it then carries
offers under their minimum. Either way it is handed over as the tool describes, and their answer is
taken as seller-comms' **Activity updates** says.

Then offer to act on them, one at a time. When the seller answers an escalation, do what they said
and close the loop with `resolve_escalation`.

If nothing is waiting, say so in one line.

$ARGUMENTS
