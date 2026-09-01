"""The stale-intent sweep — the last resort for a send the machine could not settle itself.

An intent still un-committed past the grace window is folded as `unconfirmed` and an escalation is
opened to ask the human whether it actually sent — never a re-send. The grace is held under the
pacing delay ceiling with wide margin, so a merely-jittered send can never look like a stall. The
deterministic outbound msg_id means a genuinely-retried commit stays a no-op, so this only heals a
real interruption.

Elapsed time is no longer enough on its own. The inbox lane re-reads every unsettled thread and
records the attempt, so the fold is gated on the machine having actually looked
(`MIN_VERIFY_ATTEMPTS`) — the seller is the last resort, not the first. `HARD_GRACE_SEC` still folds
regardless, for the one failure waiting cannot fix: a lane that never runs leaves the attempt count
at zero forever, and an unconfirmed message to a real buyer cannot sit there indefinitely. That
fold gets its own wording: an ask claiming the chat was re-checked when nothing checked it is a
claim about work nobody did, and it is the part the seller is right to object to.
"""

from __future__ import annotations

from sellee.store.send import HARD_GRACE_SEC, MIN_VERIFY_ATTEMPTS

# Legacy journal_reconcile.GRACE_SEC — the delay ceiling is held well under this so a healthy
# reserve→send window can never reach the fold floor.
GRACE_SEC = 600.0

__all__ = ["GRACE_SEC", "HARD_GRACE_SEC", "MIN_VERIFY_ATTEMPTS", "run_stale_intent_sweep"]


def run_stale_intent_sweep(
    *,
    bus,
    store,
    grace_sec: float = GRACE_SEC,
    now: float | None = None,
    min_verify_attempts: int = MIN_VERIFY_ATTEMPTS,
    hard_grace_sec: float = HARD_GRACE_SEC,
):
    """Fold stale intents and publish an event per fold (plus escalation.open for any new one)."""
    folded = store.stale_intent_sweep(
        grace_sec,
        now=now,
        min_verify_attempts=min_verify_attempts,
        hard_grace_sec=hard_grace_sec,
    )
    for entry in folded:
        bus.publish(
            "intent.unconfirmed",
            {
                "intent_id": entry["intent_id"],
                "thread_id": entry["thread_id"],
                # Whether the machine had actually looked before it asked — the one field that says
                # whether an ask on the seller's phone was earned or was a timeout wearing its coat.
                "looked": entry["looked"],
            },
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
