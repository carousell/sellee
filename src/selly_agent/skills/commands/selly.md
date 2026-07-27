---
description: Status, settings, and what needs the seller
---

Show the seller where things stand, then offer to act. In one pass:

1. `get_status` — daemon health, paused or not, whether Telegram is bound.
2. `list_items` — what's live and what's still a draft.
3. `get_catchup` — anything waiting on them. Render it the same way as `/catchup`.
4. `get_settings` — their settings, current values first; mention the ones still at defaults only
   if they ask.

Render it as one compact summary, not four sections of tool output. Then offer the obvious next
moves: answer an open escalation, change a setting, pause or resume.

To change a setting, use `propose_setting_change`. You can only propose — some settings apply
straight away and some wait for the seller's approval; the tool's answer says which happened, so
report that rather than assuming it took effect.

$ARGUMENTS
