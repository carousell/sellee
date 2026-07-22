"""The stale-intent sweep — a scheduler task that heals a send that may have been interrupted.

An intent still un-committed past the grace window is folded as `unconfirmed` and an escalation is
opened to ask the human whether it actually sent — never a re-send. The grace is held under the
pacing delay ceiling with wide margin, so a merely-jittered send can never look like a stall. The
deterministic outbound msg_id means a genuinely-retried commit stays a no-op, so this only heals a
real interruption.
"""

from __future__ import annotations

# Legacy journal_reconcile.GRACE_SEC — the delay ceiling is held well under this so a healthy
# reserve→send window can never reach the fold floor.
GRACE_SEC = 600.0


def run_stale_intent_sweep(*, bus, store, grace_sec: float = GRACE_SEC, now: float | None = None):
    """Fold stale intents and publish an event per fold (plus escalation.open for any new one)."""
    folded = store.stale_intent_sweep(grace_sec, now=now)
    for entry in folded:
        bus.publish(
            "intent.unconfirmed",
            {"intent_id": entry["intent_id"], "thread_id": entry["thread_id"]},
        )
        if entry["escalation_new"]:
            bus.publish(
                "escalation.open",
                {
                    "id": entry["escalation_id"],
                    "thread_id": entry["thread_id"],
                    "kind": "unconfirmed_send",
                },
            )
    return folded
